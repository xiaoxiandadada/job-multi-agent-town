from job_agent_harness.model_client import OpenAICompatibleClient
from job_agent_harness.models import RoleSpec


def make_role(profile: str) -> RoleSpec:
    return RoleSpec(
        role_id="test_role",
        display_name="测试角色",
        goal="验证角色级模型配置可以正确路由",
        system_prompt="只输出用于测试的简短结论。",
        model_profile=profile,
    )


def test_model_profiles_route_roles_without_changing_the_orchestrator():
    client = OpenAICompatibleClient(
        base_url="https://example.com/v1",
        api_key="test-key",
        default_model="worker-model",
        judge_model="judge-model",
        knowledge_model="knowledge-model",
        reliable_model="reliable-model",
    )

    assert client.model_for(make_role("default")) == "worker-model"
    assert client.model_for(make_role("knowledge")) == "knowledge-model"
    assert client.model_for(make_role("reliable")) == "reliable-model"
    assert client.model_for(make_role("judge")) == "judge-model"
    assert client.model_for(make_role("custom-fallback")) == "worker-model"


def test_knowledge_profile_defaults_to_judge_model(monkeypatch):
    monkeypatch.delenv("JOB_AGENT_KNOWLEDGE_MODEL", raising=False)
    monkeypatch.delenv("JOB_AGENT_RELIABLE_MODEL", raising=False)
    client = OpenAICompatibleClient(
        base_url="https://example.com/v1",
        api_key="test-key",
        default_model="worker-model",
        judge_model="judge-model",
    )

    assert client.model_for(make_role("knowledge")) == "judge-model"
    assert client.model_for(make_role("reliable")) == "judge-model"


def test_role_model_override_beats_environment_and_profile(monkeypatch):
    monkeypatch.setenv(
        "JOB_AGENT_ROLE_MODEL_TEST_ROLE",
        "environment-role-model",
    )
    client = OpenAICompatibleClient(
        base_url="https://example.com/v1",
        api_key="test-key",
        default_model="worker-model",
    )
    role = make_role("default")

    assert client.model_for(role) == "environment-role-model"
    assert client.model_for(
        role.model_copy(update={"model": "registry-role-model"})
    ) == "registry-role-model"


def test_local_doc_role_receives_versioned_project_context(tmp_path):
    context_path = tmp_path / "project-context.md"
    context_path.write_text(
        "技术栈：FastAPI 和 LangGraph；不是 Flask。",
        encoding="utf-8",
    )
    role = make_role("reliable").model_copy(
        update={"tools": ["local_docs"]}
    )
    client = OpenAICompatibleClient(
        base_url="https://example.com/v1",
        api_key="test-key",
        default_model="worker-model",
        project_context_path=context_path,
        prepare_dir=tmp_path,
    )

    prompt = client.system_prompt_for(role)

    assert role.system_prompt in prompt
    assert "FastAPI 和 LangGraph" in prompt
    assert "不得补写" in prompt


def test_role_without_local_docs_does_not_receive_project_context(tmp_path):
    context_path = tmp_path / "project-context.md"
    context_path.write_text("不应注入", encoding="utf-8")
    role = make_role("reliable")
    client = OpenAICompatibleClient(
        base_url="https://example.com/v1",
        api_key="test-key",
        default_model="worker-model",
        project_context_path=context_path,
        prepare_dir=tmp_path,
    )

    assert client.system_prompt_for(role) == role.system_prompt


def test_judge_always_receives_project_context(tmp_path):
    context_path = tmp_path / "project-context.md"
    context_path.write_text("只保留仓库内证据。", encoding="utf-8")
    role = make_role("judge").model_copy(update={"role_id": "judge"})
    client = OpenAICompatibleClient(
        base_url="https://example.com/v1",
        api_key="test-key",
        default_model="worker-model",
        project_context_path=context_path,
        prepare_dir=tmp_path,
    )

    assert "只保留仓库内证据" in client.system_prompt_for(role)


def test_job_scout_receives_latest_daily_and_job_tracker(tmp_path):
    (tmp_path / "daily").mkdir()
    (tmp_path / "jobs").mkdir()
    (tmp_path / "daily" / "2026-07-30.md").write_text(
        "# 日报\n新增：百度 2027 届岗位",
        encoding="utf-8",
    )
    (tmp_path / "jobs" / "autumn_job_tracker.md").write_text(
        "官方 JD：https://example.com/job",
        encoding="utf-8",
    )
    role = RoleSpec(
        role_id="job_scout",
        display_name="岗位侦察员",
        goal="发现并核验中国 2027 届正式校招岗位",
        system_prompt="只输出有来源的岗位。",
        tools=["web_search"],
    )
    client = OpenAICompatibleClient(
        base_url="https://example.com/v1",
        api_key="test-key",
        default_model="worker-model",
        prepare_dir=tmp_path,
        project_context_path=tmp_path / "missing.md",
        seed_roles_path=tmp_path / "missing.json",
    )

    prompt = client.system_prompt_for(role)

    assert "百度 2027 届岗位" in prompt
    assert "https://example.com/job" in prompt
    assert "最新求职资料包" in prompt
