from datetime import datetime, timedelta, timezone

from job_agent_harness.cognition import MemoryStore


def test_memory_retrieval_combines_relevance_recency_and_importance(tmp_path):
    store = MemoryStore(tmp_path / "memories.jsonl")
    relevant = store.append(
        role_id="portfolio_coach",
        run_id="run-1",
        kind="observation",
        text="Agent Evaluation 使用 golden set 与工具失败分类",
        importance=0.72,
    )
    store.append(
        role_id="portfolio_coach",
        run_id="run-2",
        kind="observation",
        text="简历排版需要统一字体",
        importance=0.4,
    )

    results = store.retrieve(
        role_id="portfolio_coach",
        query="如何构建 Agent Evaluation golden set",
        limit=2,
        now=datetime.now(timezone.utc) + timedelta(minutes=1),
    )

    assert results[0].memory.memory_id == relevant.memory_id
    assert results[0].relevance > results[1].relevance
    assert 0 <= results[0].score <= 1


def test_memory_store_creates_reflection_after_observation_threshold(tmp_path):
    store = MemoryStore(tmp_path / "memories.jsonl", reflection_interval=3)
    for index in range(2):
        store.append(
            role_id="jd_analyst",
            run_id=f"run-{index}",
            kind="observation",
            text=f"第 {index} 次分析 Agent 岗位，部分要求待核验",
        )
    assert store.maybe_reflect(
        role_id="jd_analyst",
        run_id="run-2",
    ) is None

    store.append(
        role_id="jd_analyst",
        run_id="run-2",
        kind="observation",
        text="第三次分析 Agent 岗位，发现评测指标缺口",
    )
    reflection = store.maybe_reflect(
        role_id="jd_analyst",
        run_id="run-2",
    )

    assert reflection is not None
    assert reflection.kind == "reflection"
    assert "下一轮先核验" in reflection.text
    assert store.maybe_reflect(
        role_id="jd_analyst",
        run_id="run-3",
    ) is None


def test_plan_is_persisted_as_agent_memory(tmp_path):
    store = MemoryStore(tmp_path / "memories.jsonl")

    plan = store.ensure_plan(
        role_id="job_scout",
        run_id="run-1",
        schedule=["核验届别", "检查官方链接"],
        query="这个岗位仍待核验",
    )

    assert plan.kind == "plan"
    assert "核验届别 → 检查官方链接" in plan.text
    assert "优先核验" in plan.text


def test_reflection_uses_whole_domain_terms_and_treats_timeout_as_risk(
    tmp_path,
):
    store = MemoryStore(tmp_path / "memories.jsonl", reflection_interval=3)
    for text in (
        "timeout: agent timed out",
        "为 LangGraph RPG 求职 Agent 小镇添加角色创建界面和本地存储",
        "最重要的验收指标是角色信息可持久化",
    ):
        store.append(
            role_id="portfolio_coach",
            run_id="run-1",
            kind="observation",
            text=text,
        )

    reflection = store.maybe_reflect(
        role_id="portfolio_coach",
        run_id="run-1",
    )

    assert reflection is not None
    assert "LangGraph" in reflection.text
    assert "角色创建" in reflection.text
    focus = reflection.text.split("。代表性经验", 1)[0]
    assert "、创建界" not in focus
    assert "下一轮先核验风险" in reflection.text
