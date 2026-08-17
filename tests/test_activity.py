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
            make_role("material_builder"),
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
            requested_roles=["job_scout", "material_builder"],
            mode="collaborative",
        ),
        thread_id="activity-run",
    )
    events = activity.read(run_id="activity-run")

    assert events[0].kind == "run_started"
    assert events[-1].kind == "run_completed"
    assert {event.phase for event in events} >= {
        "route",
        "discovery",
        "action",
        "judge",
    }
    handoff = next(
        event for event in events if event.kind == "handoff_created"
    )
    assert handoff.source_role_ids == ["job_scout"]
    assert handoff.target_role_ids == ["material_builder"]
    completed = [
        event for event in events if event.kind == "agent_completed"
    ]
    assert {event.role_id for event in completed} == {
        "job_scout",
        "material_builder",
        "judge",
    }
    assert all(event.model for event in completed)
    assert all(event.latency_ms is not None for event in completed)
    assert events[-1].metrics["model_calls"] == 3
    assert report.run_id == "activity-run"


async def test_run_started_says_who_asked_for_the_run(tmp_path):
    """The page shows one run at a time and has to know which one to drop.

    A patrol dispatches an ordinary single-role run, so while it is still in
    flight nothing else distinguishes it from a request the user typed.
    """

    registry = RoleRegistry(tmp_path / "roles.json")
    registry.replace_all([make_role("job_scout")])
    activity = ActivityStore(tmp_path / "activity.jsonl", excerpt_chars=90)
    base = MultiAgentOrchestrator(
        registry,
        MockModelClient(),
        activity_store=activity,
    )

    typed = await base.run(
        RunRequest(query="检索岗位", requested_roles=["job_scout"], use_judge=False)
    )
    patrol = await base.run(
        RunRequest(
            query="巡检岗位是否还开放",
            requested_roles=["job_scout"],
            use_judge=False,
            origin="patrol",
        )
    )
    # The same stamp has to survive the graph wrapper, which is what actually
    # runs in production.
    graphed = await LangGraphOrchestrator(base).run(
        RunRequest(
            query="巡检岗位是否还开放",
            requested_roles=["job_scout"],
            use_judge=False,
            origin="patrol",
        )
    )

    def origin_of(run_id: str) -> str:
        started = next(
            event
            for event in activity.read(run_id=run_id)
            if event.kind == "run_started"
        )
        return started.metrics["origin"]

    assert origin_of(typed.run_id) == "user"
    assert origin_of(patrol.run_id) == "patrol"
    assert origin_of(graphed.run_id) == "patrol"


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


def test_reading_the_tail_does_not_depend_on_how_long_the_file_is(tmp_path):
    """The dashboard polls this every 1.5s and nothing ever prunes the file.

    ``_reversed_lines`` is exercised with a chunk far smaller than one event so a
    line always straddles a chunk boundary — the case a naive seek gets wrong.
    """

    from job_agent_harness import activity as activity_module

    path = tmp_path / "activity.jsonl"
    store = ActivityStore(path)
    for index in range(50):
        store.record(
            run_id=f"run-{index}",
            kind="run_started",
            status="running",
            orchestrator="langgraph",
            query=f"第 {index} 次巡检",
        )

    tail = [event.run_id for event in store.read(limit=3)]
    assert tail == ["run-47", "run-48", "run-49"]

    lines = list(activity_module._reversed_lines(path, chunk_size=8))
    assert len(lines) == 50
    assert [event.run_id for event in store.read(limit=50)][0] == "run-0"


def test_the_tail_read_survives_a_file_with_no_trailing_newline(tmp_path):
    path = tmp_path / "activity.jsonl"
    store = ActivityStore(path)
    store.record(
        run_id="one",
        kind="run_started",
        status="running",
        orchestrator="asyncio",
    )
    text = path.read_text(encoding="utf-8")
    path.write_text(text.rstrip("\n"), encoding="utf-8")

    assert [event.run_id for event in store.read(limit=5)] == ["one"]
