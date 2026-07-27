from job_agent_harness.activity import ActivityEvent
from job_agent_harness.models import RoleSpec
from job_agent_harness.registry import RoleRegistry
from job_agent_harness.town import build_town_snapshot


def make_role(role_id: str, display_name: str) -> RoleSpec:
    return RoleSpec(
        role_id=role_id,
        display_name=display_name,
        goal=f"完成 {display_name} 的可核验任务",
        system_prompt=f"你是 {display_name}，只输出可核验内容。",
    )


def test_town_snapshot_maps_live_run_to_agents_and_memories(tmp_path):
    registry = RoleRegistry(tmp_path / "roles.json")
    registry.replace_all(
        [
            make_role("job_scout", "岗位侦察员"),
            make_role("resume_strategist", "简历策略师"),
            make_role("judge", "证据审核员"),
        ]
    )
    events = [
        ActivityEvent(
            run_id="run-1",
            kind="run_started",
            status="running",
            orchestrator="langgraph",
            phase="route",
            query_excerpt="分析 AI Agent 岗位",
        ),
        ActivityEvent(
            run_id="run-1",
            kind="route_completed",
            status="completed",
            orchestrator="langgraph",
            phase="route",
            selected_role_ids=["job_scout", "resume_strategist"],
        ),
        ActivityEvent(
            run_id="run-1",
            kind="agent_completed",
            status="ok",
            orchestrator="langgraph",
            phase="context",
            role_id="job_scout",
            display_name="岗位侦察员",
            model="worker-model",
            latency_ms=1200,
            output_excerpt="发现一个待核验岗位",
        ),
        ActivityEvent(
            run_id="run-1",
            kind="handoff_created",
            status="completed",
            orchestrator="langgraph",
            phase="action",
            source_role_ids=["job_scout"],
            target_role_ids=["resume_strategist"],
            output_excerpt="共享岗位证据",
        ),
        ActivityEvent(
            run_id="run-1",
            kind="phase_started",
            status="running",
            orchestrator="langgraph",
            phase="action",
            selected_role_ids=["resume_strategist"],
        ),
        ActivityEvent(
            run_id="run-1",
            kind="agent_started",
            status="running",
            orchestrator="langgraph",
            phase="action",
            role_id="resume_strategist",
            display_name="简历策略师",
        ),
    ]

    snapshot = build_town_snapshot(registry, events)
    by_role = {agent.role_id: agent for agent in snapshot.agents}

    assert snapshot.current_run_id == "run-1"
    assert snapshot.current_phase == "action"
    assert by_role["job_scout"].status == "ok"
    assert by_role["job_scout"].place == "机会驿站"
    assert by_role["resume_strategist"].status == "running"
    assert "action" in by_role["resume_strategist"].current_action
    assert any(
        memory.kind == "handoff_created"
        for memory in by_role["resume_strategist"].memories
    )
    assert snapshot.handoffs[0].target_role_ids == [
        "resume_strategist"
    ]


def test_town_snapshot_is_idle_without_activity(tmp_path):
    registry = RoleRegistry(tmp_path / "roles.json")
    registry.replace_all([make_role("job_scout", "岗位侦察员")])

    snapshot = build_town_snapshot(registry, [])

    assert snapshot.current_run_id is None
    assert snapshot.current_phase == "idle"
    assert snapshot.agents[0].status == "idle"
    assert snapshot.timeline == []
