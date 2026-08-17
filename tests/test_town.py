from datetime import datetime, timedelta, timezone

from job_agent_harness.activity import ActivityEvent
from job_agent_harness.models import RoleSpec
from job_agent_harness.registry import RoleRegistry
from job_agent_harness.town import (
    background_run_ids,
    build_town_snapshot,
    current_run_id_for_events,
)
from job_agent_harness.tasks import build_run_task_graph


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
            make_role("job_scout", "Job Scout"),
            make_role("material_builder", "Material Builder"),
            make_role("judge", "Evidence Judge"),
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
            selected_role_ids=["job_scout", "material_builder"],
        ),
        ActivityEvent(
            run_id="run-1",
            kind="agent_completed",
            status="ok",
            orchestrator="langgraph",
            phase="context",
            role_id="job_scout",
            display_name="Job Scout",
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
            target_role_ids=["material_builder"],
            output_excerpt="共享岗位证据",
        ),
        ActivityEvent(
            run_id="run-1",
            kind="phase_started",
            status="running",
            orchestrator="langgraph",
            phase="action",
            selected_role_ids=["material_builder"],
        ),
        ActivityEvent(
            run_id="run-1",
            kind="agent_started",
            status="running",
            orchestrator="langgraph",
            phase="action",
            role_id="material_builder",
            display_name="Material Builder",
        ),
    ]

    snapshot = build_town_snapshot(registry, events)
    by_role = {agent.role_id: agent for agent in snapshot.agents}

    assert snapshot.current_run_id == "run-1"
    assert snapshot.current_phase == "action"
    assert by_role["job_scout"].status == "ok"
    assert by_role["job_scout"].place == "Scout Outpost"
    assert by_role["material_builder"].status == "running"
    assert "action" in by_role["material_builder"].current_action
    assert any(
        memory.kind == "handoff_created"
        for memory in by_role["material_builder"].memories
    )
    assert snapshot.handoffs[0].target_role_ids == [
        "material_builder"
    ]


def test_town_snapshot_is_idle_without_activity(tmp_path):
    registry = RoleRegistry(tmp_path / "roles.json")
    registry.replace_all([make_role("job_scout", "Job Scout")])

    snapshot = build_town_snapshot(registry, [])

    assert snapshot.current_run_id is None
    assert snapshot.current_phase == "idle"
    assert snapshot.agents[0].status == "idle"
    assert snapshot.timeline == []


def test_town_snapshot_exposes_historical_replay_position(tmp_path):
    registry = RoleRegistry(tmp_path / "roles.json")
    registry.replace_all([make_role("job_scout", "Job Scout")])
    events = [
        ActivityEvent(
            timestamp="2026-07-28T01:00:00+00:00",
            run_id="run-replay",
            kind="run_started",
            status="running",
            orchestrator="langgraph",
            phase="route",
            query_excerpt="回放岗位任务",
        ),
        ActivityEvent(
            timestamp="2026-07-28T01:00:02+00:00",
            run_id="run-replay",
            kind="route_completed",
            status="completed",
            orchestrator="langgraph",
            phase="route",
            selected_role_ids=["job_scout"],
        ),
    ]

    snapshot = build_town_snapshot(
        registry,
        events,
        replay_step=2,
        replay_total_steps=5,
    )

    assert snapshot.replay.enabled is True
    assert snapshot.replay.step == 2
    assert snapshot.replay.total_steps == 5
    assert snapshot.town_time == "2026-07-28 09:00:02"
    assert snapshot.agents[0].status == "queued"


def test_town_replay_projects_task_dependencies_without_future_status(
    tmp_path,
):
    scout = make_role("job_scout", "Job Scout")
    resume = make_role("material_builder", "Material Builder").model_copy(
        update={"workflow_stage": "action"}
    )
    registry = RoleRegistry(tmp_path / "roles.json")
    registry.replace_all([scout, resume])
    graph = build_run_task_graph(
        run_id="task-replay",
        query="先找岗位再改简历",
        roles=[scout, resume],
        mode="collaborative",
        use_judge=False,
    )
    final_graph = graph.model_copy(deep=True)
    for task in final_graph.tasks:
        task.status = "completed"
        task.progress = 100
    final_graph.status = "completed"
    final_graph.progress = 100
    events = [
        ActivityEvent(
            timestamp="2026-07-28T01:00:00+00:00",
            run_id="task-replay",
            kind="run_started",
            status="running",
            orchestrator="langgraph",
            phase="route",
        ),
        ActivityEvent(
            timestamp="2026-07-28T01:00:01+00:00",
            run_id="task-replay",
            kind="task_started",
            status="running",
            orchestrator="langgraph",
            phase="discovery",
            role_id="job_scout",
            task_id="job_scout",
            task_title="核验岗位",
            progress=50,
        ),
    ]

    snapshot = build_town_snapshot(
        registry,
        events,
        replay_step=2,
        replay_total_steps=8,
        task_graph=final_graph,
    )
    by_id = {
        task.task_id: task for task in snapshot.task_graph.tasks
    }

    assert by_id["job_scout"].status == "running"
    assert by_id["job_scout"].progress == 50
    assert by_id["material_builder"].status == "blocked"
    assert snapshot.task_graph.progress == 25


def test_routing_edges_come_from_real_dispatch_handoff_and_review(tmp_path):
    registry = RoleRegistry(tmp_path / "roles.json")
    registry.replace_all(
        [
            make_role("job_scout", "Job Scout"),
            make_role("material_builder", "Material Builder"),
            make_role("judge", "Evidence Judge"),
        ]
    )
    events = [
        ActivityEvent(
            timestamp="2026-08-03T01:00:00+00:00",
            run_id="run-9",
            kind="route_completed",
            status="completed",
            orchestrator="langgraph",
            phase="route",
            selected_role_ids=["job_scout", "material_builder"],
        ),
        ActivityEvent(
            timestamp="2026-08-03T01:00:05+00:00",
            run_id="run-9",
            kind="agent_completed",
            status="ok",
            orchestrator="langgraph",
            phase="discovery",
            role_id="job_scout",
            output_excerpt="核验到一个岗位",
        ),
        ActivityEvent(
            timestamp="2026-08-03T01:00:06+00:00",
            run_id="run-9",
            kind="handoff_created",
            status="completed",
            orchestrator="langgraph",
            phase="action",
            source_role_ids=["job_scout"],
            target_role_ids=["material_builder"],
            output_excerpt="共享岗位证据",
        ),
        ActivityEvent(
            timestamp="2026-08-03T01:00:20+00:00",
            run_id="run-9",
            kind="agent_completed",
            status="ok",
            orchestrator="langgraph",
            phase="action",
            role_id="material_builder",
            output_excerpt="给出简历版本",
        ),
        ActivityEvent(
            timestamp="2026-08-03T01:00:21+00:00",
            run_id="run-9",
            kind="agent_started",
            status="running",
            orchestrator="langgraph",
            phase="judge",
            role_id="judge",
        ),
    ]

    snapshot = build_town_snapshot(registry, events)
    edges = {
        (route.kind, route.source_role_id, route.target_role_id)
        for route in snapshot.routes
    }

    assert ("dispatch", "plaza", "job_scout") in edges
    assert ("handoff", "job_scout", "material_builder") in edges
    assert ("review", "job_scout", "judge") in edges
    assert ("review", "material_builder", "judge") in edges
    # The judge never hands work back to itself.
    assert ("review", "judge", "judge") not in edges
    assert snapshot.routes == sorted(
        snapshot.routes, key=lambda route: route.timestamp
    )


def test_no_review_edge_before_the_judge_actually_starts(tmp_path):
    registry = RoleRegistry(tmp_path / "roles.json")
    registry.replace_all(
        [make_role("job_scout", "Job Scout"), make_role("judge", "Evidence Judge")]
    )
    events = [
        ActivityEvent(
            run_id="run-10",
            kind="agent_completed",
            status="ok",
            orchestrator="langgraph",
            phase="discovery",
            role_id="job_scout",
        )
    ]

    snapshot = build_town_snapshot(registry, events)

    assert [route.kind for route in snapshot.routes] == []


def test_a_running_patrol_keeps_the_building_working(tmp_path):
    registry = RoleRegistry(tmp_path / "roles.json")
    registry.replace_all([make_role("job_scout", "Job Scout")])
    now = datetime.now(timezone.utc)
    events = [
        ActivityEvent(
            timestamp=(now - timedelta(seconds=40)).isoformat(),
            run_id="patrol-abc",
            kind="patrol_started",
            status="running",
            orchestrator="always_on",
            phase="patrol",
            role_id="job_scout",
            query_excerpt="核验今天新增的正式校招岗位",
            selected_role_ids=["job_scout"],
        )
    ]

    snapshot = build_town_snapshot(registry, events)
    shift = snapshot.shifts[0]

    assert snapshot.current_phase == "patrol"
    assert snapshot.agents[0].status == "running"
    assert "常驻巡检" in snapshot.agents[0].current_action
    assert shift.state == "working"
    assert shift.patrols == 0
    assert shift.last_error is None


def test_a_heartbeat_does_not_hijack_the_current_run(tmp_path):
    registry = RoleRegistry(tmp_path / "roles.json")
    registry.replace_all([make_role("job_scout", "Job Scout")])
    now = datetime.now(timezone.utc)
    events = [
        ActivityEvent(
            timestamp=(now - timedelta(seconds=120)).isoformat(),
            run_id="patrol-abc",
            kind="patrol_completed",
            status="ok",
            orchestrator="always_on",
            phase="patrol",
            role_id="job_scout",
            latency_ms=151000,
            output_excerpt="14 个岗位已核验",
            selected_role_ids=["job_scout"],
        ),
        ActivityEvent(
            timestamp=(now - timedelta(seconds=5)).isoformat(),
            run_id="always-on",
            kind="heartbeat",
            status="idle",
            orchestrator="always_on",
            phase="patrol",
            selected_role_ids=["job_scout"],
            metrics={"next_patrol_in_seconds": 780.0, "patrols": 3},
        ),
    ]

    snapshot = build_town_snapshot(registry, events)
    shift = snapshot.shifts[0]

    assert snapshot.current_run_id == "patrol-abc"
    assert snapshot.agents[0].status == "ok"
    assert shift.state == "standby"
    assert shift.next_patrol_in_seconds == 780.0
    assert shift.heartbeat_age_seconds is not None
    assert shift.heartbeat_age_seconds < 30
    assert 100 < shift.seconds_since_last_patrol < 200
    assert "下一轮巡检还有" in shift.last_summary


def test_a_silent_watcher_is_reported_offline_not_on_duty(tmp_path):
    registry = RoleRegistry(tmp_path / "roles.json")
    registry.replace_all([make_role("job_scout", "Job Scout")])
    stale = datetime.now(timezone.utc) - timedelta(hours=2)
    events = [
        ActivityEvent(
            timestamp=stale.isoformat(),
            run_id="patrol-dead",
            kind="patrol_completed",
            status="error",
            orchestrator="always_on",
            phase="patrol",
            role_id="job_scout",
            error="job_scout: timeout agent timed out",
            selected_role_ids=["job_scout"],
            metrics={"consecutive_failures": 2},
        )
    ]

    snapshot = build_town_snapshot(registry, events)
    shift = snapshot.shifts[0]

    assert shift.state == "offline"
    assert shift.consecutive_failures == 2
    assert shift.last_error is not None
    assert "常驻巡检失败" in shift.last_summary


def test_a_patrol_does_not_steal_the_town_from_a_running_task(tmp_path):
    """The bug this guards: the header showed 2/7, then 1/1, then 2/7 again.

    A patrol fires every few minutes and dispatches one role. Taking the newest
    run meant the town and the task board jumped to it while the user's
    collaborative run was still mid-flight.
    """

    registry = RoleRegistry(tmp_path / "roles.json")
    registry.replace_all(
        [
            make_role("job_scout", "Job Scout"),
            make_role("job_analyst", "Job Analyst"),
        ]
    )
    base = datetime(2026, 8, 4, 0, 0, tzinfo=timezone.utc)

    def at(offset: int, **values) -> ActivityEvent:
        return ActivityEvent(
            timestamp=(base + timedelta(seconds=offset)).isoformat(),
            orchestrator=values.pop("orchestrator", "langgraph"),
            **values,
        )

    events = [
        at(0, run_id="user-run", kind="run_started", status="running", phase="route",
           query_excerpt="分析 AI Agent 岗位", metrics={"origin": "user"}),
        at(1, run_id="user-run", kind="phase_started", status="running",
           phase="discovery"),
        at(1, run_id="user-run", kind="agent_started", status="running",
           phase="discovery", role_id="job_scout"),
        # The watcher wakes up and hands a single role an ordinary run.
        at(2, run_id="patrol-abc", kind="patrol_started", status="running",
           orchestrator="always_on", phase="patrol", role_id="job_scout"),
        at(3, run_id="patrol-inner", kind="run_started", status="running",
           phase="route", query_excerpt="巡检岗位是否还开放",
           metrics={"origin": "patrol"}),
        at(4, run_id="patrol-inner", kind="agent_started", status="running",
           phase="action", role_id="job_scout"),
        at(5, run_id="always-on", kind="heartbeat", status="idle",
           orchestrator="always_on", phase="patrol"),
    ]

    assert current_run_id_for_events(events) == "user-run"
    # Both halves of the patrol are recognised, including the run it dispatched.
    assert background_run_ids(events) == {"patrol-abc", "patrol-inner", "always-on"}
    # The town still describes the user's run, not the one-role patrol.
    snapshot = build_town_snapshot(registry, events)
    assert snapshot.current_run_id == "user-run"
    assert snapshot.current_phase == "discovery"


def test_the_roles_writing_the_daily_brief_also_stay_off_the_stage(tmp_path):
    """Seven back-to-back single-role runs, none of which anyone requested.

    The morning brief is generated one section at a time. Each of those runs is
    an ordinary single-role run, so without the ``schedule`` mark each would take
    the header in turn while the user's own task was still going.
    """

    registry = RoleRegistry(tmp_path / "roles.json")
    registry.replace_all(
        [
            make_role("job_scout", "Job Scout"),
            make_role("judge", "Evidence Judge"),
        ]
    )
    base = datetime(2026, 8, 5, 1, 30, tzinfo=timezone.utc)

    def at(offset: int, **values) -> ActivityEvent:
        return ActivityEvent(
            timestamp=(base + timedelta(seconds=offset)).isoformat(),
            orchestrator=values.pop("orchestrator", "langgraph"),
            **values,
        )

    events = [
        at(0, run_id="user-run", kind="run_started", status="running",
           phase="route", metrics={"origin": "user"}),
        at(1, run_id="user-run", kind="phase_started", status="running",
           phase="discovery"),
        at(2, run_id="brief-scout", kind="run_started", status="running",
           phase="route", query_excerpt="今日秋招维护结果",
           metrics={"origin": "schedule"}),
        at(3, run_id="brief-scout", kind="agent_completed", status="ok",
           phase="action", role_id="job_scout"),
        at(4, run_id="brief-judge", kind="run_started", status="running",
           phase="route", query_excerpt="今天先做三件事",
           metrics={"origin": "schedule"}),
    ]

    assert background_run_ids(events) == {"brief-scout", "brief-judge"}
    assert current_run_id_for_events(events) == "user-run"
    assert build_town_snapshot(registry, events).current_run_id == "user-run"


def test_a_patrol_still_shows_up_when_nothing_else_ever_ran(tmp_path):
    """Otherwise a freshly started studio looks broken instead of idle."""

    registry = RoleRegistry(tmp_path / "roles.json")
    registry.replace_all([make_role("job_scout", "Job Scout")])
    base = datetime(2026, 8, 4, 0, 0, tzinfo=timezone.utc)

    events = [
        ActivityEvent(
            timestamp=(base + timedelta(seconds=offset)).isoformat(),
            run_id=run_id,
            kind=kind,
            status="running",
            orchestrator=orchestrator,
            phase=phase,
            role_id="job_scout",
        )
        for offset, run_id, kind, orchestrator, phase in [
            (0, "patrol-abc", "patrol_started", "always_on", "patrol"),
            (1, "patrol-inner", "run_started", "langgraph", "route"),
            (2, "patrol-inner", "agent_started", "langgraph", "action"),
        ]
    ]
    events[1].metrics = {"origin": "patrol"}

    # The watcher's own shift, not the run it dispatched: the "patrol" phase is
    # what lets the routing panel say why it has nothing to show.
    assert current_run_id_for_events(events) == "patrol-abc"
    assert build_town_snapshot(registry, events).current_phase == "patrol"
