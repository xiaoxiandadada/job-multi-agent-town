"""Does the memory store actually return the memory that answers the question?

Every other number in this repo measures the harness: how long a run took, how
many roles ran in parallel, whether the Judge changed the answer. Nothing measured
retrieval, and retrieval is the one place where being wrong is invisible — a role
gets four memories, none of them relevant, and still writes a confident answer.
The failure shows up as "the agent forgot the Baidu job it verified yesterday",
which reads like a model problem and is not one.

The trick that makes this measurable without hand-labelling is that this project's
memories are full of *identifiers*: job ids like ``J100679``, official hostnames,
company names. A query about a job id has an objectively correct answer set — every
memory whose text contains that id — so relevance is a predicate, not an opinion.
That is much weaker than LoCoMo-style human labels, and it is worth being explicit
about why it is still the right call here: the alternative is no measurement at all,
and an identifier match cannot flatter the retriever the way a judge model can.

The metrics are the standard retrieval ones, one per case and then averaged:

``recall@k``
    Of the memories that do answer the query, how many made it into the top k.
``hit@k``
    Whether *any* of them did. A role only needs one good memory to stop guessing,
    so this is the number that maps to "did the agent remember".
``precision@k``
    How much of the role's memory budget was spent on relevant text.
``boilerplate@k``
    Share of the top k that is templated bookkeeping rather than evidence. Not a
    standard IR metric; it is here because a store that writes a "本轮计划：…" line
    on every run can score respectably on recall while handing the model four
    copies of the same sentence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from statistics import mean

from .cognition import AgentMemory, MemoryStore


#: Memory kinds that carry evidence a role can act on. ``plan`` is excluded on
#: purpose: it is generated from ``RoleSpec.schedule`` before the role has done
#: anything, so it can restate the schedule and nothing else.
EVIDENCE_KINDS = frozenset({"observation", "handoff", "reflection"})

#: What "boilerplate" means for this project, as prefixes actually written by
#: ``MemoryStore.ensure_plan`` and ``maybe_reflect``.
BOILERPLATE_PREFIXES = ("本轮计划：", "阶段反思：")


@dataclass(frozen=True)
class RetrievalCase:
    """One query plus the objective test for "this memory answers it".

    ``required`` holds substrings that must *all* appear for a memory to count.
    Using a conjunction keeps a case like "百度的大模型岗 J100679" from being
    answered by any memory that merely says 百度.
    """

    query: str
    required: tuple[str, ...]
    role_id: str = "job_scout"
    note: str = ""

    def matches(self, memory: AgentMemory) -> bool:
        text = memory.text.casefold()
        return all(item.casefold() in text for item in self.required)


@dataclass
class CaseScore:
    case: RetrievalCase
    relevant_total: int
    retrieved_relevant: int
    retrieved_total: int
    boilerplate: int
    k: int = 4
    top_kinds: list[str] = field(default_factory=list)

    @property
    def recall(self) -> float:
        if not self.relevant_total:
            return 0.0
        return self.retrieved_relevant / self.relevant_total

    @property
    def recall_ceiling(self) -> float:
        """Best recall reachable at this k.

        A patrol that revisits the same job every morning leaves 40 memories
        naming it, and 4 slots cannot hold 40. Without this, ``recall@4`` looks
        like a failing grade when it is within a few points of arithmetically
        perfect, and the only way to "improve" it would be to raise k until the
        role's context is all memory.
        """

        if not self.relevant_total:
            return 0.0
        return min(self.k, self.relevant_total) / self.relevant_total

    @property
    def hit(self) -> float:
        return 1.0 if self.retrieved_relevant else 0.0

    @property
    def precision(self) -> float:
        if not self.retrieved_total:
            return 0.0
        return self.retrieved_relevant / self.retrieved_total

    @property
    def boilerplate_rate(self) -> float:
        if not self.retrieved_total:
            return 0.0
        return self.boilerplate / self.retrieved_total


@dataclass
class EvalReport:
    k: int
    scores: list[CaseScore]
    #: Cases whose relevant set is empty in this store. Reported rather than
    #: silently averaged in, because a 0.0 from "nothing to find" and a 0.0 from
    #: "found nothing" are different failures and only one is the retriever's.
    skipped: list[RetrievalCase] = field(default_factory=list)

    def _mean(self, attribute: str) -> float:
        if not self.scores:
            return 0.0
        return mean(getattr(score, attribute) for score in self.scores)

    @property
    def recall(self) -> float:
        return self._mean("recall")

    @property
    def recall_ceiling(self) -> float:
        return self._mean("recall_ceiling")

    @property
    def hit_rate(self) -> float:
        return self._mean("hit")

    @property
    def precision(self) -> float:
        return self._mean("precision")

    @property
    def boilerplate_rate(self) -> float:
        return self._mean("boilerplate_rate")

    def as_dict(self) -> dict[str, object]:
        return {
            "k": self.k,
            "cases": len(self.scores),
            "skipped": len(self.skipped),
            "recall_at_k": round(self.recall, 4),
            "recall_ceiling_at_k": round(self.recall_ceiling, 4),
            "hit_at_k": round(self.hit_rate, 4),
            "precision_at_k": round(self.precision, 4),
            "boilerplate_at_k": round(self.boilerplate_rate, 4),
        }


def is_boilerplate(memory: AgentMemory) -> bool:
    return memory.text.startswith(BOILERPLATE_PREFIXES)


def evaluate_retrieval(
    store: MemoryStore,
    cases: list[RetrievalCase],
    *,
    k: int = 4,
) -> EvalReport:
    """Score ``store.retrieve`` against every case that has an answer in it."""

    scores: list[CaseScore] = []
    skipped: list[RetrievalCase] = []
    for case in cases:
        pool = [
            memory
            for memory in store.list(role_id=case.role_id, limit=2000)
            if memory.kind in EVIDENCE_KINDS
        ]
        relevant = {memory.memory_id for memory in pool if case.matches(memory)}
        if not relevant:
            skipped.append(case)
            continue
        retrieved = store.retrieve(
            role_id=case.role_id,
            query=case.query,
            limit=k,
        )
        scores.append(
            CaseScore(
                case=case,
                relevant_total=len(relevant),
                k=k,
                retrieved_relevant=sum(
                    item.memory.memory_id in relevant for item in retrieved
                ),
                retrieved_total=len(retrieved),
                boilerplate=sum(
                    is_boilerplate(item.memory) for item in retrieved
                ),
                top_kinds=[item.memory.kind for item in retrieved],
            )
        )
    return EvalReport(k=k, scores=scores, skipped=skipped)


_JOB_ID = re.compile(r"\b[A-Z]\d{5,7}\b")


def discover_cases(
    store: MemoryStore,
    *,
    role_id: str = "job_scout",
    min_occurrences: int = 2,
    limit: int = 12,
) -> list[RetrievalCase]:
    """Build cases from the job ids the store already contains.

    A golden set that has to be maintained by hand rots the moment the job table
    changes, and this one has to survive a store that grows every morning. Job ids
    are the most specific thing in the corpus, so deriving cases from them keeps
    the eval honest about *this* store instead of a snapshot of it.

    ``min_occurrences`` skips ids that appear in exactly one memory: those make
    recall a coin flip on a single document rather than a measurement.
    """

    counts: dict[str, int] = {}
    for memory in store.list(role_id=role_id, limit=2000):
        if memory.kind not in EVIDENCE_KINDS:
            continue
        for job_id in set(_JOB_ID.findall(memory.text)):
            counts[job_id] = counts.get(job_id, 0) + 1
    ranked = sorted(
        (item for item in counts.items() if item[1] >= min_occurrences),
        key=lambda item: (-item[1], item[0]),
    )
    return [
        RetrievalCase(
            query=(
                f"岗位 {job_id} 的官方链接、届别和截止时间是什么？"
                "之前核验过的结论直接给我。"
            ),
            required=(job_id,),
            role_id=role_id,
            note=f"{count} 条记忆提到该岗位",
        )
        for job_id, count in ranked[:limit]
    ]
