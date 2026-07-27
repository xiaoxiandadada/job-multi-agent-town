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
