from fastapi.testclient import TestClient

from job_agent_harness.model_client import MockModelClient
from job_agent_harness.orchestrator import MultiAgentOrchestrator
from job_agent_harness.api import create_app


def test_role_can_be_added_without_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_AGENT_DATA_DIR", str(tmp_path))
    client = TestClient(create_app())
    before = client.get("/api/roles").json()

    response = client.post(
        "/api/roles",
        json={
            "role_id": "bioinformatics_coach",
            "display_name": "生信算法教练",
            "goal": "把生信岗位要求映射到项目证据",
            "system_prompt": "根据已有证据输出技能差距和作品建议。",
            "trigger_keywords": ["生信", "GWAS"],
            "tools": ["local_docs"],
            "model_profile": "default",
            "enabled": True,
        },
    )

    assert response.status_code == 201
    assert response.json()["registry_version"] == 2
    after = client.get("/api/roles").json()
    assert len(after) == len(before) + 1
    assert after[-1]["role_id"] == "bioinformatics_coach"


def test_role_can_be_paused_and_reconfigured_without_restart(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("JOB_AGENT_DATA_DIR", str(tmp_path))
    client = TestClient(create_app())

    response = client.patch(
        "/api/roles/portfolio_coach",
        json={
            "enabled": False,
            "town_place": "作品实验室",
            "workflow_stage": "action",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["role"]["enabled"] is False
    assert payload["role"]["town_place"] == "作品实验室"
    town = client.get("/api/town").json()
    agent = next(
        item
        for item in town["agents"]
        if item["role_id"] == "portfolio_coach"
    )
    assert agent["status"] == "disabled"
    assert agent["place"] == "作品实验室"


def test_agent_memory_search_endpoint_uses_persisted_run_memory(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("JOB_AGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        "job_agent_harness.api.build_orchestrator",
        lambda registry, memory_store: MultiAgentOrchestrator(
            registry,
            MockModelClient(),
            memory_store=memory_store,
        ),
    )
    client = TestClient(create_app())
    response = client.post(
        "/api/runs",
        json={
            "query": "分析 Agent Evaluation golden set",
            "requested_roles": ["portfolio_coach"],
            "mode": "single",
            "use_judge": False,
        },
    )

    assert response.status_code == 200
    memories = client.get(
        "/api/agents/portfolio_coach/memories"
    ).json()
    assert {memory["kind"] for memory in memories} >= {
        "plan",
        "observation",
    }
    results = client.get(
        "/api/agents/portfolio_coach/memories/search",
        params={"query": "golden set"},
    ).json()
    assert results
    assert "score" in results[0]
