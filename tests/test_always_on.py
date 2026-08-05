import asyncio

import pytest

from job_agent_harness.activity import ActivityStore
from job_agent_harness.always_on import (
    AlwaysOnWatcher,
    SingleInstanceLock,
)
from job_agent_harness.models import AgentResult, RunMetrics, RunReport


def make_report(run_id: str = "run-1", *, results=None, final_output=None) -> RunReport:
    failed = [item for item in (results or []) if item.status != "ok"]
    return RunReport(
        run_id=run_id,
        query="巡检",
        mode="single",
        role_registry_version=1,
        results=list(results or []),
        final_output=(
            "百度 J100679 仍然开放，JD 原文未变。"
            if final_output is None
            else final_output
        ),
        metrics=RunMetrics(
            selected_roles=1,
            completed_roles=1 - len(failed),
            failed_roles=len(failed),
            model_calls=1,
            wall_latency_ms=1234.0,
            sum_agent_latency_ms=1234.0,
            input_tokens=100,
            output_tokens=200,
            parallel_speedup_estimate=1.0,
        ),
    )


class FakeOrchestrator:
    def __init__(self, outcomes=None):
        self.outcomes = list(outcomes or [])
        self.requests = []

    async def run(self, request):
        self.requests.append(request)
        outcome = (
            self.outcomes.pop(0) if self.outcomes else make_report(f"run-{len(self.requests)}")
        )
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class FakeClock:
    """Drives the watcher through N sleeps, then stops the loop."""

    def __init__(self, stop_after: int):
        self.stop_after = stop_after
        self.sleeps: list[float] = []
        self.now = 0.0

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if len(self.sleeps) >= self.stop_after:
            # A cancelled sleep never finished, so the clock does not advance.
            raise asyncio.CancelledError
        self.now += seconds
        await asyncio.sleep(0)

    def monotonic(self) -> float:
        return self.now


def build_watcher(tmp_path, orchestrator, clock, **changes):
    options = {
        "orchestrator": orchestrator,
        "activity_store": ActivityStore(tmp_path / "activity.jsonl"),
        "roles": ["job_scout"],
        "interval_seconds": 900.0,
        "jitter_seconds": 0.0,
        # Large enough that a wait is one sleep, so the test reads the delay
        # the watcher chose rather than a heartbeat slice of it.
        "heartbeat_seconds": 1_000_000.0,
        "first_delay_seconds": 10.0,
        "sleep": clock.sleep,
        "monotonic": clock.monotonic,
        "jitter": lambda span: 0.0,
    }
    options.update(changes)
    return AlwaysOnWatcher(**options)


async def run_loop(watcher):
    with pytest.raises(asyncio.CancelledError):
        await watcher.loop()


async def test_patrol_runs_a_real_single_role_run(tmp_path):
    orchestrator = FakeOrchestrator()
    clock = FakeClock(stop_after=2)
    watcher = build_watcher(tmp_path, orchestrator, clock)

    await run_loop(watcher)

    assert len(orchestrator.requests) == 1
    request = orchestrator.requests[0]
    assert request.requested_roles == ["job_scout"]
    assert request.mode == "single"
    assert request.use_judge is False
    assert "岗位" in request.query
    # The page has one "current run" slot. Without this mark, the run a patrol
    # dispatches is indistinguishable from a one-role request the user typed,
    # and it shoves the collaborative run they are watching off the screen.
    assert request.origin == "patrol"


async def test_patrol_emits_started_and_completed_events(tmp_path):
    orchestrator = FakeOrchestrator()
    clock = FakeClock(stop_after=2)
    watcher = build_watcher(tmp_path, orchestrator, clock)

    await run_loop(watcher)

    events = watcher.activity_store.read()
    kinds = [event.kind for event in events]
    assert kinds == ["patrol_started", "patrol_completed"]
    completed = events[-1]
    assert completed.status == "ok"
    assert completed.role_id == "job_scout"
    assert "J100679" in completed.output_excerpt
    assert completed.metrics["source_run_id"] == "run-1"


async def test_consecutive_patrols_rotate_the_prompt(tmp_path):
    orchestrator = FakeOrchestrator()
    clock = FakeClock(stop_after=3)
    watcher = build_watcher(tmp_path, orchestrator, clock)

    await run_loop(watcher)

    queries = [request.query for request in orchestrator.requests]
    assert len(queries) == 2
    assert queries[0] != queries[1]


async def test_failures_back_off_and_are_recorded(tmp_path):
    orchestrator = FakeOrchestrator([RuntimeError("网关 500"), RuntimeError("网关 500")])
    clock = FakeClock(stop_after=3)
    watcher = build_watcher(tmp_path, orchestrator, clock)

    await run_loop(watcher)

    # first_delay, then 900 * 2 after one failure, then 900 * 4 after two.
    assert clock.sleeps == [10.0, 1800.0, 3600.0]
    assert watcher.failures == 2
    assert watcher.last_error == "网关 500"
    failed = [
        event
        for event in watcher.activity_store.read()
        if event.kind == "patrol_completed"
    ]
    assert [event.status for event in failed] == ["error", "error"]
    assert failed[0].error == "网关 500"


async def test_backoff_is_capped(tmp_path):
    watcher = build_watcher(
        tmp_path,
        FakeOrchestrator(),
        FakeClock(stop_after=1),
        max_backoff_seconds=2000.0,
    )
    watcher.failures = 6

    assert watcher.next_delay() == 2000.0


async def test_a_success_clears_the_backoff(tmp_path):
    orchestrator = FakeOrchestrator([RuntimeError("网关 500"), make_report()])
    clock = FakeClock(stop_after=3)
    watcher = build_watcher(tmp_path, orchestrator, clock)

    await run_loop(watcher)

    assert clock.sleeps == [10.0, 1800.0, 900.0]
    assert watcher.failures == 0
    assert watcher.last_error is None


async def test_heartbeats_prove_the_watcher_is_alive_while_idle(tmp_path):
    orchestrator = FakeOrchestrator()
    clock = FakeClock(stop_after=4)
    watcher = build_watcher(
        tmp_path,
        orchestrator,
        clock,
        first_delay_seconds=0.0,
        heartbeat_seconds=300.0,
    )

    await run_loop(watcher)

    beats = [
        event
        for event in watcher.activity_store.read()
        if event.kind == "heartbeat"
    ]
    assert len(beats) >= 2
    assert beats[0].status == "idle"
    assert beats[0].selected_role_ids == ["job_scout"]
    assert beats[0].metrics["next_patrol_in_seconds"] == 600.0
    assert beats[1].metrics["next_patrol_in_seconds"] == 300.0


async def test_status_reports_how_long_since_the_last_patrol(tmp_path):
    orchestrator = FakeOrchestrator()
    clock = FakeClock(stop_after=2)
    watcher = build_watcher(tmp_path, orchestrator, clock)

    await run_loop(watcher)
    clock.now += 42.0
    status = watcher.status()

    assert status["patrols"] == 1
    assert status["roles"] == ["job_scout"]
    assert status["seconds_since_last_patrol"] == 42.0
    assert status["consecutive_failures"] == 0


def test_watcher_is_disabled_without_roles(tmp_path):
    watcher = build_watcher(
        tmp_path, FakeOrchestrator(), FakeClock(stop_after=1), roles=[]
    )

    assert watcher.enabled is False
    assert watcher.start() is False


async def test_only_one_process_holds_the_shift(tmp_path):
    lock_path = tmp_path / "always_on.lock"
    first = build_watcher(
        tmp_path,
        FakeOrchestrator(),
        FakeClock(stop_after=50),
        lock_path=lock_path,
    )
    second = build_watcher(
        tmp_path,
        FakeOrchestrator(),
        FakeClock(stop_after=50),
        lock_path=lock_path,
    )

    assert first.start() is True
    assert second.start() is False
    assert second.running is False

    await first.stop()
    assert second.start() is True
    await second.stop()


def test_lock_is_released_on_stop(tmp_path):
    lock = SingleInstanceLock(tmp_path / "l.lock")

    assert lock.acquire() is True
    assert lock.held is True
    lock.release()
    assert lock.held is False
    assert lock.acquire() is True
    lock.release()


async def test_patrol_passes_its_own_generous_timeout(tmp_path):
    """A patrol grounds a JD over several tool rounds; 45 s is not enough."""

    orchestrator = FakeOrchestrator()
    watcher = build_watcher(
        tmp_path, orchestrator, FakeClock(stop_after=2), timeout_seconds=300.0
    )

    await run_loop(watcher)

    assert orchestrator.requests[0].timeout_seconds == 300.0
    assert watcher.status()["timeout_seconds"] == 300.0


async def test_a_run_whose_agent_timed_out_is_not_a_green_patrol(tmp_path):
    """Seen live: run_completed, agent timed out, zero output tokens.

    Reporting that as ok showed a green patrol that produced nothing, and kept
    the backoff from ever engaging.
    """

    timed_out = make_report(
        results=[
            AgentResult(
                role_id="job_scout",
                display_name="Job Scout",
                status="timeout",
                error="agent timed out",
            )
        ],
        final_output="",
    )
    orchestrator = FakeOrchestrator([timed_out])
    clock = FakeClock(stop_after=2)
    watcher = build_watcher(tmp_path, orchestrator, clock)

    await run_loop(watcher)

    completed = [
        event
        for event in watcher.activity_store.read()
        if event.kind == "patrol_completed"
    ]
    assert [event.status for event in completed] == ["error"]
    assert "agent timed out" in completed[0].error
    assert completed[0].metrics["source_run_id"] == "run-1"
    assert watcher.failures == 1
    # A failed patrol must not advance the "worked N s ago" clock either.
    assert watcher.status()["seconds_since_last_patrol"] is None


async def test_an_empty_answer_also_counts_as_a_failed_patrol(tmp_path):
    orchestrator = FakeOrchestrator([make_report(final_output="   ")])
    watcher = build_watcher(tmp_path, orchestrator, FakeClock(stop_after=2))

    await run_loop(watcher)

    assert watcher.failures == 1
    assert watcher.last_error == "巡检没有产出任何内容"
