from fastapi.testclient import TestClient

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

