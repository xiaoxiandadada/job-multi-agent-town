"""The eval has to be able to fail, or it is decoration.

These tests mostly pin the ways a retrieval metric quietly lies: averaging in
cases that had no answer to find, counting boilerplate as a win, and reporting a
recall number without the ceiling that ``k`` imposes on it.
"""

from job_agent_harness.cognition import MemoryStore
from job_agent_harness.memory_eval import (
    RetrievalCase,
    discover_cases,
    evaluate_retrieval,
    is_boilerplate,
)


def build_store(tmp_path) -> MemoryStore:
    store = MemoryStore(tmp_path / "memories.jsonl")
    store.append(
        role_id="job_scout",
        run_id="run-1",
        kind="observation",
        text=(
            "官方岗位证据 - 百度大模型研发工程师（J100679）"
            "官方链接：https://talent.baidu.com/jd/J100679 届别：2027 届。"
        ),
    )
    store.append(
        role_id="job_scout",
        run_id="run-2",
        kind="observation",
        text="J100679 的投递截止时间页面未标注，正文需人工核验。",
    )
    store.append(
        role_id="job_scout",
        run_id="run-3",
        kind="observation",
        text="拼多多算法岗 J101017 官方页面可访问，届别 2027 届。",
    )
    return store


def test_a_case_with_no_answer_in_the_store_is_skipped_not_scored(tmp_path):
    """A 0.0 from "nothing to find" is not the retriever's failure."""

    store = build_store(tmp_path)
    cases = [
        RetrievalCase(query="岗位 J100679 的官方链接", required=("J100679",)),
        RetrievalCase(query="岗位 J999999 的官方链接", required=("J999999",)),
    ]

    report = evaluate_retrieval(store, cases, k=4)

    assert len(report.scores) == 1
    assert [case.required for case in report.skipped] == [("J999999",)]
    assert report.hit_rate == 1.0


def test_required_substrings_are_a_conjunction(tmp_path):
    """Otherwise "百度的 J100679" is answered by any memory mentioning 百度."""

    store = build_store(tmp_path)
    case = RetrievalCase(
        query="百度那个大模型岗",
        required=("百度", "J100679"),
    )
    memories = store.list(role_id="job_scout", limit=10)

    matched = [memory.text[:12] for memory in memories if case.matches(memory)]

    assert len(matched) == 1
    assert matched[0].startswith("官方岗位证据")


def test_recall_ceiling_reports_what_k_makes_reachable(tmp_path):
    store = build_store(tmp_path)
    case = RetrievalCase(query="岗位 J100679", required=("J100679",))

    at_one = evaluate_retrieval(store, [case], k=1)
    at_four = evaluate_retrieval(store, [case], k=4)

    # Two memories name J100679, so one slot can hold at most half of them.
    assert at_one.recall_ceiling == 0.5
    assert at_four.recall_ceiling == 1.0
    assert at_one.recall <= at_one.recall_ceiling
    assert at_four.recall <= at_four.recall_ceiling


def test_boilerplate_counts_templated_bookkeeping_only(tmp_path):
    store = build_store(tmp_path)
    plan = store.ensure_plan(
        role_id="job_scout",
        run_id="run-4",
        schedule=["核验届别"],
        query="核验",
    )
    evidence = store.list(role_id="job_scout", kind="observation", limit=1)[0]

    assert is_boilerplate(plan)
    assert not is_boilerplate(evidence)


def test_discovered_cases_ignore_job_ids_seen_only_once(tmp_path):
    """One document makes recall a coin flip rather than a measurement."""

    store = build_store(tmp_path)

    cases = discover_cases(store, role_id="job_scout")

    assert [case.required[0] for case in cases] == ["J100679"]


def test_discovery_returns_nothing_for_a_role_with_no_memories(tmp_path):
    store = build_store(tmp_path)

    assert discover_cases(store, role_id="material_builder") == []
