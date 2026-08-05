from __future__ import annotations

import json
import math
import os
import re
import threading
import uuid
from collections import Counter
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


MemoryKind = Literal["observation", "handoff", "plan", "reflection"]

#: Kinds a query can retrieve. ``plan`` is written before the role has done
#: anything, from a template, so it can only ever tell the model what its own
#: config already says.
RETRIEVABLE_KINDS: frozenset[str] = frozenset(
    {"observation", "handoff", "reflection"}
)

#: Score weights. Relevance carries the most because the other two are nearly
#: constant across this store: almost everything is recent (the roles run every
#: few minutes) and ``estimate_importance`` gives most memories 0.5–1.0. A signal
#: that does not vary cannot rank.
RECENCY_WEIGHT = 0.22
RELEVANCE_WEIGHT = 0.56
IMPORTANCE_WEIGHT = 0.22

#: Above this mutual overlap, two memories are the same memory for retrieval
#: purposes. Tuned against the real store, where consecutive reflections differ
#: only in which hostname they name.
DUPLICATE_THRESHOLD = 0.82

#: An ascii token mixing letters and digits: job ids (``J100679``), run ids,
#: model versions, requisition numbers. In this corpus these are the only tokens
#: that name one specific thing.
IDENTIFIER_TOKEN = re.compile(
    r"^(?=[a-z0-9_+.-]*[a-z])(?=[a-z0-9_+.-]*\d)[a-z0-9][a-z0-9_+.-]{4,}$"
)

#: How much more an identifier match is worth than an ordinary token of the same
#: rarity. Needed because the tokenizer cuts Chinese into overlapping n-grams, so
#: a 30-character question yields ~80 tokens and the rarest of them are phrasing
#: artefacts ("之前核", "是什么") rather than content: on the real store those
#: outweighed the job id being asked about 3.71 to 2.04, and rarity alone cannot
#: tell them apart — the phrasing genuinely is rare in a corpus of job reports.
#: Measured on the 12-case golden set, hit@4 is 0.833 at weight 1.0 and 1.000
#: anywhere in 2.0–8.0, so this is a plateau rather than a fitted constant.
IDENTIFIER_WEIGHT = 4.0

DOMAIN_TERMS = (
    "Agent Evaluation",
    "golden set",
    "AI for Science",
    "LangGraph",
    "多智能体",
    "AI Agent",
    "本地存储",
    "角色创建",
    "验收指标",
    "用户输入",
    "工具调用",
    "失败分类",
    "长期记忆",
    "证据交接",
    "数据科学",
    "数据分析",
    "生信算法",
    "大模型",
    "作品证据",
    "官方链接",
    "校园招聘",
    "RAG",
    "RPG",
    "SQL",
    "简历",
    "岗位",
    "小镇",
)
ASCII_STOPWORDS = {
    "and",
    "for",
    "from",
    "http",
    "https",
    "output",
    "run",
    "the",
    "this",
    "with",
}


class AgentMemory(BaseModel):
    memory_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    role_id: str
    run_id: str
    kind: MemoryKind
    text: str
    importance: float = Field(default=0.5, ge=0, le=1)


class RetrievedMemory(BaseModel):
    memory: AgentMemory
    score: float = Field(ge=0, le=1)
    recency: float = Field(ge=0, le=1)
    relevance: float = Field(ge=0, le=1)
    importance: float = Field(ge=0, le=1)


def _tokens(value: str) -> set[str]:
    lowered = value.casefold()
    ascii_words = set(re.findall(r"[a-z0-9_+#.-]{2,}", lowered))
    chinese = "".join(re.findall(r"[\u4e00-\u9fff]", lowered))
    grams = {
        chinese[index : index + size]
        for size in (1, 2, 3)
        for index in range(max(0, len(chinese) - size + 1))
    }
    return ascii_words | grams


def _similarity(left: str, right: str) -> float:
    """Symmetric overlap, used for spotting near-duplicate memories.

    Kept length-normalised on both sides on purpose: two texts are duplicates of
    each other only if they are *mutually* similar. This is the wrong measure for
    query relevance \u2014 see ``_relevance``.
    """

    return _token_similarity(_tokens(left), _tokens(right))


def _token_similarity(left: set[str], right: set[str]) -> float:
    """``_similarity`` for callers that already tokenised.

    Retrieval compares every candidate against every memory already picked, so
    re-tokenising 4000-character texts inside that loop would make the dedup pass
    cost more than the ranking it filters.
    """

    if not left or not right:
        return 0.0
    return len(left & right) / math.sqrt(len(left) * len(right))


def inverse_document_frequency(
    documents: list[set[str]],
) -> dict[str, float]:
    """How rare each token is in one role's own memory.

    Without this, every token counts the same, and in a corpus of job reports
    that means \u5c97\u4f4d and \u6838\u9a8c \u2014 which are in literally every memory \u2014 outvote the
    one token that identifies the job being asked about. The store is small
    enough (hundreds of memories) that computing this per retrieval is cheaper
    than keeping an index in sync with an append-only file.
    """

    total = len(documents)
    frequency: Counter[str] = Counter()
    for tokens in documents:
        frequency.update(tokens)
    return {
        token: math.log(1 + total / (1 + count))
        for token, count in frequency.items()
    }


def _relevance(
    query_tokens: set[str],
    document_tokens: set[str],
    idf: dict[str, float],
) -> float:
    """Share of the question, weighted by rarity, that this memory can answer.

    Deliberately **asymmetric**. The previous measure divided by
    ``sqrt(len(query) * len(document))``, which made the score depend mostly on
    how long the memory was: a 4000-character report that quoted the exact job id
    and its official link could not score above ~0.08, while a 33-character
    "\u672c\u8f6e\u8ba1\u5212\uff1a\u2026" template reached 0.15 by sharing a few common characters. The
    retriever was therefore anti-correlated with information content \u2014 it ranked
    the store's most useful memories last, and the roles behaved as if they had
    forgotten every job they had verified. A query's length is fixed, so
    normalising by the query alone leaves a long, on-topic memory able to win.

    Identifier tokens are weighted up (see ``IDENTIFIER_WEIGHT``). Tokens the
    corpus does not contain at all fall out of the budget on their own, since
    ``idf`` is built from the documents and ``.get`` returns 0 for the rest — an
    unanswerable token should not lower every candidate's score.
    """

    if not query_tokens or not document_tokens:
        return 0.0
    weights = {
        token: idf.get(token, 0.0)
        * (IDENTIFIER_WEIGHT if IDENTIFIER_TOKEN.match(token) else 1.0)
        for token in query_tokens
    }
    budget = sum(weights.values())
    if budget <= 0:
        return 0.0
    matched = sum(weights[token] for token in query_tokens & document_tokens)
    return min(1.0, matched / budget)


def estimate_importance(text: str, kind: MemoryKind) -> float:
    base = {
        "observation": 0.45,
        "handoff": 0.68,
        "plan": 0.55,
        "reflection": 0.85,
    }[kind]
    evidence_bonus = 0.08 if re.search(
        r"https?://|证据|通过|失败|超时|风险|待核验|指标|截止",
        text,
        re.IGNORECASE,
    ) else 0
    detail_bonus = min(0.12, len(text) / 5000)
    return min(1.0, base + evidence_bonus + detail_bonus)


def _focus_terms(memories: list[AgentMemory], limit: int = 5) -> list[str]:
    joined = "\n".join(memory.text for memory in memories)
    lowered = joined.casefold()
    ranked: Counter[str] = Counter()
    for term in DOMAIN_TERMS:
        count = lowered.count(term.casefold())
        if count:
            ranked[term] += count * 3
    for word in re.findall(r"[a-z][a-z0-9_+#.-]{2,}", lowered):
        if word not in ASCII_STOPWORDS:
            ranked[word] += 1
    values: list[str] = []
    for term, _ in ranked.most_common():
        if any(
            term.casefold() in chosen.casefold()
            or chosen.casefold() in term.casefold()
            for chosen in values
        ):
            continue
        values.append(term)
        if len(values) >= limit:
            break
    return values


class MemoryStore:
    """Append-only role memory with deterministic retrieval and reflection.

    The implementation mirrors the practical parts of Generative Agents:
    observations are persisted, retrieval combines recency/relevance/importance,
    and repeated observations periodically form a higher-level reflection.
    It intentionally avoids hidden chain-of-thought and extra model calls.
    """

    def __init__(self, path: Path, reflection_interval: int = 3):
        self.path = path
        self.reflection_interval = max(2, reflection_interval)
        self._lock = threading.Lock()

    def append(
        self,
        *,
        role_id: str,
        run_id: str,
        kind: MemoryKind,
        text: str,
        importance: float | None = None,
    ) -> AgentMemory:
        normalized = " ".join(text.split())[:4000]
        memory = AgentMemory(
            role_id=role_id,
            run_id=run_id,
            kind=kind,
            text=normalized,
            importance=(
                estimate_importance(normalized, kind)
                if importance is None
                else importance
            ),
        )
        payload = (
            json.dumps(
                memory.model_dump(),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            descriptor = os.open(
                self.path,
                os.O_APPEND | os.O_CREAT | os.O_WRONLY,
                0o600,
            )
            try:
                os.write(descriptor, payload)
            finally:
                os.close(descriptor)
        return memory

    def list(
        self,
        *,
        role_id: str | None = None,
        kind: MemoryKind | None = None,
        limit: int = 100,
    ) -> list[AgentMemory]:
        values = [
            memory
            for memory in self._iter_memories()
            if (role_id is None or memory.role_id == role_id)
            and (kind is None or memory.kind == kind)
        ]
        return values[-max(1, min(limit, 2000)) :]

    def list_by_role(self, *, limit: int = 100) -> dict[str, list[AgentMemory]]:
        """Every role's last ``limit`` memories from one pass over the file.

        ``list`` re-reads and re-validates the whole JSONL on every call, so a
        caller that wants each role's memories — the town snapshot does, and the
        dashboard polls it every 1.5 seconds — otherwise pays for one full parse
        per role. Grouping cannot be expressed as a global ``limit`` on ``list``
        without silently dropping roles whose memories are all older than the cut.
        """

        keep = max(1, min(limit, 2000))
        buckets: dict[str, list[AgentMemory]] = {}
        for memory in self._iter_memories():
            bucket = buckets.setdefault(memory.role_id, [])
            bucket.append(memory)
            if len(bucket) > keep:
                del bucket[0]
        return buckets

    def _iter_memories(self) -> Iterator[AgentMemory]:
        if not self.path.exists():
            return
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                yield AgentMemory.model_validate_json(line)
            except (ValueError, json.JSONDecodeError):
                continue

    def retrieve(
        self,
        *,
        role_id: str,
        query: str,
        limit: int = 4,
        now: datetime | None = None,
    ) -> list[RetrievedMemory]:
        """The few memories worth spending this role's context on.

        Three things beyond scoring, all of them there because the store is
        written by the same loop that reads it and therefore fills up with its
        own output:

        * ``plan`` memories are not candidates. They are generated from
          ``RoleSpec.schedule`` *before* the role does anything, so they restate
          static configuration the system prompt already contains — yet they were
          40% of the real store and, being short, scored well.
        * near-duplicates are suppressed, because reflections are written every
          few observations and differ by a word or two. Four retrieval slots
          filled with four spellings of the same sentence is the same as having
          no memory at all.
        * ranking is by ``(score, timestamp)``, with the timestamp only breaking
          ties, so a fresh memory never outranks a relevant one.
        """

        now = now or datetime.now(timezone.utc)
        candidates = [
            memory
            for memory in self.list(role_id=role_id, limit=2000)
            if memory.kind in RETRIEVABLE_KINDS
        ]
        document_tokens = [_tokens(memory.text) for memory in candidates]
        idf = inverse_document_frequency(document_tokens)
        query_tokens = _tokens(query)
        scored: list[tuple[RetrievedMemory, set[str]]] = []
        for memory, tokens in zip(candidates, document_tokens):
            timestamp = datetime.fromisoformat(memory.timestamp)
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            age_hours = max(0.0, (now - timestamp).total_seconds() / 3600)
            recency = math.exp(-age_hours / (24 * 14))
            relevance = _relevance(query_tokens, tokens, idf)
            score = min(
                1.0,
                RECENCY_WEIGHT * recency
                + RELEVANCE_WEIGHT * relevance
                + IMPORTANCE_WEIGHT * memory.importance,
            )
            scored.append(
                (
                    RetrievedMemory(
                        memory=memory,
                        score=score,
                        recency=recency,
                        relevance=relevance,
                        importance=memory.importance,
                    ),
                    tokens,
                )
            )
        scored.sort(
            key=lambda value: (value[0].score, value[0].memory.timestamp),
            reverse=True,
        )
        wanted = max(0, min(limit, 20))
        picked: list[RetrievedMemory] = []
        picked_tokens: list[set[str]] = []
        for item, tokens in scored:
            if len(picked) >= wanted:
                break
            if any(
                _token_similarity(tokens, chosen) >= DUPLICATE_THRESHOLD
                for chosen in picked_tokens
            ):
                continue
            picked.append(item)
            picked_tokens.append(tokens)
        return picked

    def ensure_plan(
        self,
        *,
        role_id: str,
        run_id: str,
        schedule: list[str],
        query: str,
    ) -> AgentMemory:
        steps = schedule or ["理解任务", "检索证据", "提交可验证结果"]
        plan = "本轮计划：" + " → ".join(steps)
        if "待核验" in query:
            plan += "；优先核验不确定信息"
        return self.append(
            role_id=role_id,
            run_id=run_id,
            kind="plan",
            text=plan,
        )

    def maybe_reflect(
        self,
        *,
        role_id: str,
        run_id: str,
    ) -> AgentMemory | None:
        memories = self.list(role_id=role_id, limit=2000)
        last_reflection = next(
            (
                index
                for index in range(len(memories) - 1, -1, -1)
                if memories[index].kind == "reflection"
            ),
            -1,
        )
        observations = [
            memory
            for memory in memories[last_reflection + 1 :]
            if memory.kind in {"observation", "handoff"}
        ]
        if len(observations) < self.reflection_interval:
            return None

        focus = "、".join(_focus_terms(observations))
        evidence = "；".join(
            memory.text[:120] for memory in observations[-3:]
        )
        uncertainty = any(
            marker in memory.text
            for memory in observations
            for marker in (
                "待核验",
                "失败",
                "超时",
                "风险",
                "timeout",
                "error",
                "failed",
            )
        )
        next_step = (
            "下一轮先核验风险与缺失证据。"
            if uncertainty
            else "下一轮复用已验证方法并补充量化证据。"
        )
        return self.append(
            role_id=role_id,
            run_id=run_id,
            kind="reflection",
            text=(
                f"阶段反思：近期重点为 {focus or '当前角色任务'}。"
                f"代表性经验：{evidence}。{next_step}"
            ),
        )
