from __future__ import annotations

import json
import math
import os
import re
import threading
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


MemoryKind = Literal["observation", "handoff", "plan", "reflection"]
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
    left_tokens = _tokens(left)
    right_tokens = _tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / math.sqrt(
        len(left_tokens) * len(right_tokens)
    )


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
        if not self.path.exists():
            return []
        values: list[AgentMemory] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                memory = AgentMemory.model_validate_json(line)
            except (ValueError, json.JSONDecodeError):
                continue
            if role_id is not None and memory.role_id != role_id:
                continue
            if kind is not None and memory.kind != kind:
                continue
            values.append(memory)
        return values[-max(1, min(limit, 2000)) :]

    def retrieve(
        self,
        *,
        role_id: str,
        query: str,
        limit: int = 4,
        now: datetime | None = None,
    ) -> list[RetrievedMemory]:
        now = now or datetime.now(timezone.utc)
        scored: list[RetrievedMemory] = []
        for memory in self.list(role_id=role_id, limit=2000):
            timestamp = datetime.fromisoformat(memory.timestamp)
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            age_hours = max(0.0, (now - timestamp).total_seconds() / 3600)
            recency = math.exp(-age_hours / (24 * 14))
            relevance = _similarity(query, memory.text)
            score = min(
                1.0,
                0.38 * recency
                + 0.40 * relevance
                + 0.22 * memory.importance,
            )
            scored.append(
                RetrievedMemory(
                    memory=memory,
                    score=score,
                    recency=recency,
                    relevance=relevance,
                    importance=memory.importance,
                )
            )
        return sorted(
            scored,
            key=lambda value: (value.score, value.memory.timestamp),
            reverse=True,
        )[: max(0, min(limit, 20))]

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
