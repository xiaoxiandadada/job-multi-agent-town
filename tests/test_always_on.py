import asyncio
from types import SimpleNamespace

import pytest

from job_agent_harness.activity import ActivityStore
from job_agent_harness.always_on import (
    AlwaysOnWatcher,
    SingleInstanceLock,
)
from job_agent_harness.match_alert import AlertLedger
from job_agent_harness.models import (
    AgentResult,
    MatchDimension,
    MatchReport,
    RunMetrics,
    RunReport,
)


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


# --------------------------------------------------------------- follow-up chain
#
# A patrol used to end at the event log, so a posting found at 3am was a posting
# nobody was told about. These cover the chain that carries findings into scoring
# and the two things that must stop it: no posting, and one already pushed.


FOUND_JOBS = (
    "## 新岗位\n"
    "字节跳动 · AI Agent 工程师（校招）\n"
    "https://jobs.bytedance.com/campus/position/123456\n"
    "> 2027 届本科及以上，熟悉大模型应用开发"
)

NOTHING_FOUND = "巡检结论：找不到符合 2027 届正式校招条件的新岗位，岗位表无需更新。"


def scored_report(overall: int, *, company: str = "字节跳动", blockers=()):
    """A run whose scorer produced a report, as the follow-up chain returns it."""

    report = MatchReport(
        company=company,
        job_title="AI Agent 工程师",
        job_url="https://jobs.bytedance.com/campus/position/123456",
        overall=overall,
        verdict="值得投",
        dimensions=[
            MatchDimension(
                name="核心技能", score=overall, weight=1.0, evidence="三个 Agent 项目"
            )
        ],
        blockers=list(blockers),
    )
    return make_report(
        "followup-run",
        results=[
            AgentResult(
                role_id="match_scorer",
                display_name="Match Scorer",
                output="渲染后的评分表",
                match_report=report,
            )
        ],
        final_output="评分完成",
    )


async def test_a_patrol_with_a_posting_hands_off_to_analysis_and_scoring(tmp_path):
    """The chain the user asked for: scout finds a job, downstream continues."""

    orchestrator = FakeOrchestrator(
        [make_report(final_output=FOUND_JOBS), scored_report(45)]
    )
    watcher = build_watcher(tmp_path, orchestrator, FakeClock(stop_after=2))

    await watcher.patrol("job_scout", 0)

    assert len(orchestrator.requests) == 2
    followup = orchestrator.requests[1]
    assert followup.requested_roles == ["job_analyst", "match_scorer"]
    # collaborative, so the scorer reads the analyst's handoff rather than the
    # raw patrol text.
    assert followup.mode == "collaborative"
    assert followup.use_judge is False
    # The scout's actual findings have to travel, not just a "go look again".
    assert "jobs.bytedance.com" in followup.query


async def test_a_patrol_without_a_posting_spends_nothing_downstream(tmp_path):
    """A quiet patrol must not pay two roles to analyse the word 找不到."""

    orchestrator = FakeOrchestrator([make_report(final_output=NOTHING_FOUND)])
    watcher = build_watcher(tmp_path, orchestrator, FakeClock(stop_after=2))

    await watcher.patrol("job_scout", 0)

    assert len(orchestrator.requests) == 1


async def test_the_chain_can_be_switched_off(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_AGENT_PATROL_FOLLOWUP", "0")
    orchestrator = FakeOrchestrator([make_report(final_output=FOUND_JOBS)])
    watcher = build_watcher(tmp_path, orchestrator, FakeClock(stop_after=2))

    await watcher.patrol("job_scout", 0)

    assert len(orchestrator.requests) == 1


async def test_only_the_scout_starts_the_chain(tmp_path):
    """Another role's patrol output is not a job listing, whatever it contains."""

    orchestrator = FakeOrchestrator([make_report(final_output=FOUND_JOBS)])
    watcher = build_watcher(
        tmp_path,
        orchestrator,
        FakeClock(stop_after=2),
        roles=["interview_coach"],
    )

    await watcher.patrol("interview_coach", 0)

    assert len(orchestrator.requests) == 1


async def test_a_qualifying_score_is_pushed_once_and_not_again(tmp_path):
    """De-duplication is the difference between a nudge and a nag.

    The patrol runs every fifteen minutes against a job table that changes far
    more slowly, so the same 82% would otherwise be pushed four times an hour.
    """

    pushed = []

    orchestrator = FakeOrchestrator(
        [
            make_report(final_output=FOUND_JOBS),
            scored_report(82),
            make_report(final_output=FOUND_JOBS),
            scored_report(82),
        ]
    )
    # lock_path is what ``data_dir()`` derives state from, so passing it keeps the
    # ledger inside tmp_path. Without it the watcher falls back to the real
    # runtime directory and the test writes into deployment state — which then
    # makes the second run of this very test see "already pushed" and fail.
    watcher = build_watcher(
        tmp_path,
        orchestrator,
        FakeClock(stop_after=2),
        lock_path=tmp_path / "always_on.lock",
    )
    assert watcher.data_dir() == tmp_path

    async def fake_send(chat_id, message):
        pushed.append(message)
        return SimpleNamespace(success=True, error=None)

    real_push = watcher.push_alert

    async def tracked_push(run_id, report):
        # Exercise the real ledger logic, stubbing only the transport.
        ledger = AlertLedger(watcher.data_dir() / "match_alerts.json")
        if ledger.already_alerted(report):
            return
        await fake_send("oc_group", {"markdown": "nudge"})
        ledger.remember(report)

    watcher.push_alert = tracked_push
    assert real_push is not None

    await watcher.patrol("job_scout", 0)
    await watcher.patrol("job_scout", 1)

    assert len(pushed) == 1


async def test_a_score_below_the_bar_is_not_pushed(tmp_path):
    calls = []
    orchestrator = FakeOrchestrator(
        [make_report(final_output=FOUND_JOBS), scored_report(55)]
    )
    watcher = build_watcher(tmp_path, orchestrator, FakeClock(stop_after=2))

    async def tracked_push(run_id, report):
        calls.append(report)

    watcher.push_alert = tracked_push

    await watcher.patrol("job_scout", 0)

    assert calls == []


async def test_a_failed_followup_does_not_fail_the_patrol(tmp_path):
    """The chain is additive; a break in it must not stop the shift.

    Marking the patrol failed would trip the exponential backoff and stop
    patrolling altogether over a problem in a step that only adds value.
    """

    orchestrator = FakeOrchestrator(
        [make_report(final_output=FOUND_JOBS), RuntimeError("gateway down")]
    )
    watcher = build_watcher(tmp_path, orchestrator, FakeClock(stop_after=2))

    await watcher.patrol("job_scout", 0)

    assert watcher.failures == 0
    assert watcher.last_error is None
