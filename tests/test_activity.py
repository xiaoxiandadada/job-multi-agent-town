from job_agent_harness.activity import ActivityStore
from job_agent_harness.langgraph_orchestrator import LangGraphOrchestrator
from job_agent_harness.model_client import MockModelClient
from job_agent_harness.models import RoleSpec, RunRequest
from job_agent_harness.orchestrator import MultiAgentOrchestrator
from job_agent_harness.registry import RoleRegistry


def make_role(role_id: str) -> RoleSpec:
    return RoleSpec(
        role_id=role_id,
        display_name=role_id,
        goal=f"完成 {role_id} 的任务",
        system_prompt=f"你是 {role_id}，输出可核验证据。",
        trigger_keywords=[role_id],
    )


async def test_langgraph_persists_live_activity_events(tmp_path):
    registry = RoleRegistry(tmp_path / "roles.json")
    registry.replace_all(
        [
            make_role("job_scout"),
            make_role("resume_strategist"),
            make_role("judge"),
        ]
    )
    activity = ActivityStore(tmp_path / "activity.jsonl", excerpt_chars=90)
    graph = LangGraphOrchestrator(
        MultiAgentOrchestrator(
            registry,
            MockModelClient(),
            activity_store=activity,
        )
    )

    report = await graph.run(
        RunRequest(
            query="检索岗位并调整简历",
            requested_roles=["job_scout", "resume_strategist"],
            mode="collaborative",
        ),
        thread_id="activity-run",
    )
    events = activity.read(run_id="activity-run")

    assert events[0].kind == "run_started"
    assert events[-1].kind == "run_completed"
    assert {event.phase for event in events} >= {
        "route",
        "context",
        "action",
        "judge",
    }
    handoff = next(
        event for event in events if event.kind == "handoff_created"
    )
    assert handoff.source_role_ids == ["job_scout"]
    assert handoff.target_role_ids == ["resume_strategist"]
    completed = [
        event for event in events if event.kind == "agent_completed"
    ]
    assert {event.role_id for event in completed} == {
        "job_scout",
        "resume_strategist",
        "judge",
    }
    assert all(event.model for event in completed)
    assert all(event.latency_ms is not None for event in completed)
    assert events[-1].metrics["model_calls"] == 3
    assert report.run_id == "activity-run"


def test_activity_store_skips_invalid_lines_and_limits_results(tmp_path):
    path = tmp_path / "activity.jsonl"
    store = ActivityStore(path)
    store.record(
        run_id="one",
        kind="run_started",
        status="running",
        orchestrator="langgraph",
        query="a" * 600,
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write("not-json\n")
    store.record(
        run_id="two",
        kind="run_completed",
        status="completed",
        orchestrator="langgraph",
    )

    assert [event.run_id for event in store.read(limit=1)] == ["two"]
    assert [event.run_id for event in store.read(run_id="one")] == ["one"]
    assert len(store.read(run_id="one")[0].query_excerpt) <= 420
