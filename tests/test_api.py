from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from job_agent_harness.activity import ActivityEvent, ActivityStore
from job_agent_harness.model_client import MockModelClient
from job_agent_harness.orchestrator import MultiAgentOrchestrator
from job_agent_harness.api import create_app
from job_agent_harness.models import RoleSpec
from job_agent_harness.tasks import TaskGraphStore, build_run_task_graph


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
        "/api/roles/material_builder",
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
        if item["role_id"] == "material_builder"
    )
    assert agent["status"] == "disabled"
    assert agent["place"] == "作品实验室"


def test_role_model_can_be_configured_and_resolved_without_restart(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("JOB_AGENT_DATA_DIR", str(tmp_path))
    client = TestClient(create_app())

    response = client.patch(
        "/api/roles/job_analyst",
        json={"model": "Qwen/custom-knowledge-model"},
    )

    assert response.status_code == 200
    assert (
        response.json()["role"]["model"]
        == "Qwen/custom-knowledge-model"
    )
    models = client.get("/api/models").json()
    configured = next(
        item
        for item in models["roles"]
        if item["role_id"] == "job_analyst"
    )
    assert configured["resolved_model"] == "Qwen/custom-knowledge-model"


def test_role_effort_can_be_configured_and_cleared_without_restart(
    tmp_path,
    monkeypatch,
):
    """The web role card sends both fields, so clearing has to be expressible.

    ``exclude_unset`` means an explicit ``null`` clears the field while an absent
    key leaves it alone — which is what lets the card's "继承" option mean
    "send no output_config" rather than "keep whatever was there".
    """

    monkeypatch.setenv("JOB_AGENT_DATA_DIR", str(tmp_path))
    client = TestClient(create_app())

    raised = client.patch("/api/roles/material_builder", json={"effort": "xhigh"})
    assert raised.status_code == 200
    assert raised.json()["role"]["effort"] == "xhigh"

    inherited = client.patch(
        "/api/roles/material_builder",
        json={"model": None, "effort": None},
    )
    assert inherited.status_code == 200
    assert inherited.json()["role"]["effort"] is None

    # An absent key is not a clear: the seeded level survives an unrelated patch.
    client.patch("/api/roles/job_analyst", json={"town_place": "分析所"})
    role = next(
        item
        for item in client.get("/api/roles").json()
        if item["role_id"] == "job_analyst"
    )
    assert role["effort"] == "high"


def test_role_effort_rejects_a_level_the_api_does_not_have(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_AGENT_DATA_DIR", str(tmp_path))
    client = TestClient(create_app())

    response = client.patch("/api/roles/job_analyst", json={"effort": "extreme"})

    assert response.status_code == 422


def test_task_graph_api_returns_dependencies_and_progress(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("JOB_AGENT_DATA_DIR", str(tmp_path))
    roles = [
        RoleSpec(
            role_id="job_scout",
            display_name="Job Scout",
            goal="发现并核验中国 2027 届正式校招岗位",
            system_prompt="只输出有官方来源的岗位。",
            workflow_stage="context",
        ),
        RoleSpec(
            role_id="material_builder",
            display_name="Material Builder",
            goal="根据岗位证据选择简历版本并改写 bullet",
            system_prompt="只基于真实项目证据改写简历。",
            workflow_stage="action",
        ),
    ]
    store = TaskGraphStore(tmp_path / "task_graphs")
    store.create(
        build_run_task_graph(
            run_id="api-task-run",
            query="分析岗位并改写简历",
            roles=roles,
            mode="collaborative",
            use_judge=True,
        )
    )
    client = TestClient(create_app())

    response = client.get("/api/task-graphs/api-task-run")

    assert response.status_code == 200
    graph = response.json()
    assert graph["progress"] == 0
    resume = next(
        task
        for task in graph["tasks"]
        if task["role_id"] == "material_builder"
    )
    assert resume["depends_on"] == ["job_scout"]
    assert client.get("/api/task-graphs").json()[0]["run_id"] == (
        "api-task-run"
    )


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
            "requested_roles": ["material_builder"],
            "mode": "single",
            "use_judge": False,
        },
    )

    assert response.status_code == 200
    memories = client.get(
        "/api/agents/material_builder/memories"
    ).json()
    assert {memory["kind"] for memory in memories} >= {
        "plan",
        "observation",
    }
    results = client.get(
        "/api/agents/material_builder/memories/search",
        params={"query": "golden set"},
    ).json()
    assert results
    assert "score" in results[0]


def test_town_replay_endpoint_slices_one_real_run(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_AGENT_DATA_DIR", str(tmp_path))
    store = ActivityStore(tmp_path / "activity.jsonl")
    for event in [
        ActivityEvent(
            timestamp="2026-07-28T01:00:00+00:00",
            run_id="run-one",
            kind="run_started",
            status="running",
            orchestrator="langgraph",
            phase="route",
            query_excerpt="回放任务",
        ),
        ActivityEvent(
            timestamp="2026-07-28T01:00:01+00:00",
            run_id="run-one",
            kind="route_completed",
            status="completed",
            orchestrator="langgraph",
            phase="route",
            selected_role_ids=["job_scout"],
        ),
        ActivityEvent(
            timestamp="2026-07-28T01:00:02+00:00",
            run_id="run-one",
            kind="agent_started",
            status="running",
            orchestrator="langgraph",
            phase="context",
            role_id="job_scout",
            display_name="Job Scout",
        ),
        ActivityEvent(
            timestamp="2026-07-28T01:00:03+00:00",
            run_id="run-two",
            kind="run_started",
            status="running",
            orchestrator="langgraph",
            phase="route",
        ),
    ]:
        store.emit(event)
    client = TestClient(create_app())

    response = client.get(
        "/api/town",
        params={"run_id": "run-one", "step": 2},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["current_run_id"] == "run-one"
    assert payload["replay"] == {
        "enabled": True,
        "step": 2,
        "total_steps": 3,
    }
    scout = next(
        agent
        for agent in payload["agents"]
        if agent["role_id"] == "job_scout"
    )
    assert scout["status"] == "queued"
    assert client.get(
        "/api/town",
        params={"run_id": "missing"},
    ).status_code == 404


def test_always_on_status_is_exposed_for_the_dashboard(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_AGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JOB_AGENT_ALWAYS_ON", "0")
    client = TestClient(create_app())

    payload = client.get("/api/always-on").json()

    assert payload["configured"] is False
    assert payload["roles"] == ["job_scout"]
    assert payload["running"] is False
    assert payload["patrols"] == 0


def test_disabled_always_on_never_starts_a_shift(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_AGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JOB_AGENT_ALWAYS_ON", "0")

    # Entering the context manager is what runs the lifespan hook.
    with TestClient(create_app()) as client:
        assert client.get("/api/always-on").json()["running"] is False


def test_town_api_exposes_routing_edges_and_the_always_on_shift(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("JOB_AGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JOB_AGENT_ALWAYS_ON", "0")
    store = ActivityStore(tmp_path / "activity.jsonl")
    now = datetime.now(timezone.utc)
    for event in [
        ActivityEvent(
            timestamp=(now - timedelta(seconds=200)).isoformat(),
            run_id="run-routed",
            kind="route_completed",
            status="completed",
            orchestrator="langgraph",
            phase="route",
            selected_role_ids=["job_scout", "job_analyst"],
        ),
        ActivityEvent(
            timestamp=(now - timedelta(seconds=190)).isoformat(),
            run_id="run-routed",
            kind="handoff_created",
            status="completed",
            orchestrator="langgraph",
            phase="analysis",
            source_role_ids=["job_scout"],
            target_role_ids=["job_analyst"],
            output_excerpt="共享核验过的岗位",
        ),
        ActivityEvent(
            timestamp=(now - timedelta(seconds=20)).isoformat(),
            run_id="always-on",
            kind="heartbeat",
            status="idle",
            orchestrator="always_on",
            phase="patrol",
            selected_role_ids=["job_scout"],
            metrics={"next_patrol_in_seconds": 600.0},
        ),
    ]:
        store.emit(event)
    client = TestClient(create_app())

    town = client.get("/api/town").json()
    always_on = client.get("/api/always-on").json()

    # The heartbeat must not become the current run and blank the routing view.
    assert town["current_run_id"] == "run-routed"
    edges = {
        (route["kind"], route["source_role_id"], route["target_role_id"])
        for route in town["routes"]
    }
    assert ("dispatch", "plaza", "job_scout") in edges
    assert ("handoff", "job_scout", "job_analyst") in edges
    shift = next(item for item in town["shifts"] if item["role_id"] == "job_scout")
    assert shift["state"] == "standby"
    assert shift["display_name"] == "Job Scout"
    # The watcher usually lives in another process, so this endpoint has to read
    # the shift from the shared log rather than from its own status object.
    assert always_on["running"] is False
    assert always_on["shifts"][0]["role_id"] == "job_scout"
    assert always_on["shifts"][0]["next_patrol_in_seconds"] == 600.0


def test_console_is_served_as_static_files(tmp_path, monkeypatch):
    """控制台是静态文件，不经过构建：这些路径少一个，前端就是白屏。

    ``uv run job-agent-api`` 之外没有第二条启动命令，所以「能不能打开页面」只由
    这个挂载决定；Dockerfile 里的 ``COPY web ./web`` 也必须把子目录一起带上。
    """
    monkeypatch.setenv("JOB_AGENT_DATA_DIR", str(tmp_path))
    client = TestClient(create_app())

    index = client.get("/")
    assert index.status_code == 200
    assert index.headers["content-type"].startswith("text/html")
    # 侧边栏的六个页面
    for page in ("town", "graph", "roles", "memory", "patrol", "replay"):
        assert f'data-page="{page}"' in index.text

    for path, prefix in [
        ("/styles.css", "text/css"),
        ("/app.js", "text/javascript"),
        ("/chat.js", "text/javascript"),
        ("/game/town-scene.js", "text/javascript"),
        ("/vendor/phaser.min.js", "text/javascript"),
        ("/assets/kenney/tiny-town.png", "image/png"),
        ("/assets/kenney/tiny-dungeon.png", "image/png"),
    ]:
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.headers["content-type"].startswith(prefix), path

    # 静态挂载在最后，不能把 /api 吃掉。
    assert client.get("/api/roles").status_code == 200
