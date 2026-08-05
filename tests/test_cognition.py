from datetime import datetime, timedelta, timezone

from job_agent_harness.cognition import (
    MemoryStore,
    _relevance,
    inverse_document_frequency,
)


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


def test_a_long_evidence_memory_outranks_a_short_template(tmp_path):
    """The regression that made the roles look amnesiac.

    Retrieval used to divide by ``sqrt(len(query) * len(document))``, so the
    detailed report that actually answered the question scored *lower* than the
    templated plan line purely for being long.
    """

    store = MemoryStore(tmp_path / "memories.jsonl")
    evidence = store.append(
        role_id="job_scout",
        run_id="run-1",
        kind="observation",
        text=(
            "官方岗位证据 - 百度大模型研发工程师（J100679）"
            "官方链接：https://talent.baidu.com/jd/J100679 "
            "届别：2027 届校园招聘。投递截止：页面未标注。"
            + "职责与要求原文摘录，逐条对照候选人现有作品证据。" * 30
        ),
    )
    store.append(
        role_id="job_scout",
        run_id="run-2",
        kind="observation",
        text="岗位核验：本轮检查官方链接与届别。",
    )

    results = store.retrieve(
        role_id="job_scout",
        query="岗位 J100679 的官方链接和届别是什么",
        limit=2,
    )

    assert results[0].memory.memory_id == evidence.memory_id


def test_plan_memories_are_never_retrieved(tmp_path):
    store = MemoryStore(tmp_path / "memories.jsonl")
    store.ensure_plan(
        role_id="job_scout",
        run_id="run-1",
        schedule=["核验届别", "检查官方链接"],
        query="核验官方链接",
    )
    store.append(
        role_id="job_scout",
        run_id="run-1",
        kind="observation",
        text="核验结果：官方链接可访问，届别为 2027 届。",
    )

    results = store.retrieve(
        role_id="job_scout",
        query="核验届别与官方链接",
        limit=4,
    )

    assert [item.memory.kind for item in results] == ["observation"]


def test_near_duplicate_memories_do_not_fill_every_slot(tmp_path):
    """Four spellings of one sentence is the same as having no memory."""

    store = MemoryStore(tmp_path / "memories.jsonl")
    for index in range(4):
        store.append(
            role_id="jd_analyst",
            run_id=f"run-{index}",
            kind="reflection",
            text=(
                "阶段反思：近期重点为 岗位、大模型、校园招聘。"
                "代表性经验：未投递岗位巡检报告，官方页面均可访问。"
                "下一轮复用已验证方法并补充量化证据。"
            ),
            importance=0.95,
        )
    distinct = store.append(
        role_id="jd_analyst",
        run_id="run-9",
        kind="observation",
        text="简历第二版把分布式训练经验替换为多智能体编排作品证据。",
    )

    results = store.retrieve(
        role_id="jd_analyst",
        query="岗位与简历证据的进展",
        limit=4,
    )

    ids = [item.memory.memory_id for item in results]
    assert len(ids) == len(set(ids))
    assert len(results) == 2
    assert distinct.memory_id in ids


def test_an_identifier_outweighs_an_equally_rare_ordinary_token(tmp_path):
    """Why ``IDENTIFIER_WEIGHT`` exists at all.

    The n-gram tokenizer turns a 30-character question into ~80 tokens, and on the
    real store the *rarest* of them were artefacts of how the question was worded
    ("之前核", "是什么") rather than content — they outweighed the job id being
    asked about 3.71 to 2.04. Rarity cannot separate the two, because question
    phrasing genuinely is rare in a corpus of job reports. Token sets are built by
    hand here so the two candidates match exactly one query token each and only
    the weighting can decide.
    """

    documents = [{"j100679"}, {"deadline"}]
    idf = inverse_document_frequency(documents)
    assert idf["j100679"] == idf["deadline"]  # same rarity, so idf is a tie

    query = {"j100679", "deadline"}
    identifier = _relevance(query, documents[0], idf)
    ordinary = _relevance(query, documents[1], idf)

    assert identifier > ordinary


def test_grouping_by_role_matches_asking_role_by_role(tmp_path):
    """The town snapshot swapped seven full parses for one; same answer required.

    The last case is the one a global ``limit`` on ``list`` gets wrong: a role that
    stopped writing long ago still has memories, and they must not fall off the end
    just because busier roles have written more since.
    """

    store = MemoryStore(tmp_path / "memories.jsonl")
    store.append(
        role_id="interview_coach",
        run_id="run-0",
        kind="observation",
        text="很久以前的一条：面试题库已按缺口重排。",
    )
    for index in range(30):
        store.append(
            role_id="job_scout",
            run_id=f"run-{index}",
            kind="observation",
            text=f"第 {index} 轮巡检：官方页面可访问。",
        )

    grouped = store.list_by_role(limit=6)

    assert sorted(grouped) == ["interview_coach", "job_scout"]
    for role_id, memories in grouped.items():
        assert [item.memory_id for item in memories] == [
            item.memory_id
            for item in store.list(role_id=role_id, limit=6)
        ]
    assert len(grouped["job_scout"]) == 6
    assert len(grouped["interview_coach"]) == 1
