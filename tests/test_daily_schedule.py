from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from job_agent_harness.daily_schedule import DailyPushScheduler, parse_clock


SHANGHAI = ZoneInfo("Asia/Shanghai")


class Clock:
    """A hand-cranked clock, so a week of mornings costs no wall time."""

    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta) -> None:
        self.now += timedelta(**delta)


class Pushes:
    def __init__(self, *, fail_times: int = 0):
        self.dates: list[str] = []
        self.full_flags: list[bool] = []
        self.fail_times = fail_times

    async def __call__(self, target_date: str, full: bool) -> None:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("飞书网关超时")
        self.dates.append(target_date)
        self.full_flags.append(full)


def build(tmp_path, clock, pushes, **overrides) -> DailyPushScheduler:
    return DailyPushScheduler(
        push=pushes,
        data_dir=tmp_path,
        push_at=overrides.pop("push_at", "09:30"),
        timezone="Asia/Shanghai",
        now=clock,
        **overrides,
    )


def test_parse_clock_rejects_anything_that_is_not_hh_mm():
    assert parse_clock("09:30").hour == 9
    assert parse_clock(" 7:05 ").minute == 5
    for bad in ["9", "09-30", "24:00", "09:60", "早上"]:
        with pytest.raises(ValueError):
            parse_clock(bad)


async def test_nothing_goes_out_before_the_slot(tmp_path):
    clock = Clock(datetime(2026, 8, 5, 8, 0, tzinfo=SHANGHAI))
    pushes = Pushes()
    scheduler = build(tmp_path, clock, pushes)

    assert scheduler.due_date(clock()) is None
    assert await scheduler.tick() is None
    assert pushes.dates == []
    assert scheduler.next_slot(clock()).hour == 9


async def test_it_pushes_at_the_slot_and_only_once_that_day(tmp_path):
    clock = Clock(datetime(2026, 8, 5, 9, 30, tzinfo=SHANGHAI))
    pushes = Pushes()
    scheduler = build(tmp_path, clock, pushes)

    assert await scheduler.tick() == "2026-08-05"
    # Every later poll of the same day must be a no-op, including one a minute
    # later — the state file, not process memory, is what says "already sent".
    for hours in [0, 1, 3, 10]:
        clock.advance(hours=hours, minutes=1)
        assert await scheduler.tick() is None
    assert pushes.dates == ["2026-08-05"]
    assert pushes.full_flags == [False]
    assert scheduler.last_pushed_date() == "2026-08-05"


async def test_a_laptop_that_woke_up_late_still_gets_the_brief(tmp_path):
    """The lid was shut at 09:30. Nothing fired. It opens at 11:00.

    A scheduler that slept until an exact instant would have missed the day
    entirely; this is the case the polling design exists for.
    """

    clock = Clock(datetime(2026, 8, 5, 11, 0, tzinfo=SHANGHAI))
    pushes = Pushes()
    scheduler = build(tmp_path, clock, pushes)

    assert await scheduler.tick() == "2026-08-05"
    assert pushes.dates == ["2026-08-05"]


async def test_a_brief_that_went_stale_overnight_is_dropped_not_pushed(
    tmp_path,
):
    """A 09:30 brief delivered at 23:00 is noise, so that day is written off."""

    clock = Clock(datetime(2026, 8, 5, 23, 0, tzinfo=SHANGHAI))
    pushes = Pushes()
    scheduler = build(tmp_path, clock, pushes, max_delay_hours=6)

    assert await scheduler.tick() is None
    assert pushes.dates == []
    # Skipping a day must not poison the next one.
    clock.advance(hours=11)
    assert await scheduler.tick() == "2026-08-06"


async def test_a_restart_one_minute_later_does_not_push_twice(tmp_path):
    clock = Clock(datetime(2026, 8, 5, 9, 30, tzinfo=SHANGHAI))
    pushes = Pushes()
    assert await build(tmp_path, clock, pushes).tick() == "2026-08-05"

    clock.advance(minutes=1)
    # A brand new object, as after `uv run job-agent-api` was restarted.
    assert await build(tmp_path, clock, pushes).tick() is None
    assert pushes.dates == ["2026-08-05"]


async def test_the_next_morning_pushes_again(tmp_path):
    clock = Clock(datetime(2026, 8, 5, 9, 30, tzinfo=SHANGHAI))
    pushes = Pushes()
    scheduler = build(tmp_path, clock, pushes)

    assert await scheduler.tick() == "2026-08-05"
    clock.advance(days=1)
    assert await scheduler.tick() == "2026-08-06"
    assert pushes.dates == ["2026-08-05", "2026-08-06"]


async def test_a_failed_push_backs_off_then_retries_inside_the_window(
    tmp_path,
):
    clock = Clock(datetime(2026, 8, 5, 9, 30, tzinfo=SHANGHAI))
    pushes = Pushes(fail_times=1)
    scheduler = build(tmp_path, clock, pushes, retry_seconds=600)

    assert await scheduler.tick() is None
    assert scheduler.failures == 1
    assert scheduler.last_error is not None
    # Not remembered as pushed, but not retried on the very next poll either.
    assert scheduler.last_pushed_date() is None
    clock.advance(minutes=2)
    assert await scheduler.tick() is None
    assert pushes.fail_times == 0

    clock.advance(minutes=10)
    assert await scheduler.tick() == "2026-08-05"
    assert scheduler.failures == 0
    assert scheduler.last_error is None


async def test_a_failure_that_never_recovers_stops_at_the_window_edge(
    tmp_path,
):
    clock = Clock(datetime(2026, 8, 5, 9, 30, tzinfo=SHANGHAI))
    pushes = Pushes(fail_times=99)
    scheduler = build(
        tmp_path,
        clock,
        pushes,
        retry_seconds=600,
        max_delay_hours=2,
    )

    for _ in range(12):
        assert await scheduler.tick() is None
        clock.advance(minutes=11)
    # Past 11:30 the window is shut; it stops trying instead of retrying all day.
    assert clock().hour >= 11
    assert scheduler.due_date(clock()) is None


async def test_only_one_process_takes_the_job(tmp_path):
    clock = Clock(datetime(2026, 8, 5, 9, 0, tzinfo=SHANGHAI))
    first = build(tmp_path, clock, Pushes())
    second = build(tmp_path, clock, Pushes())

    assert first.start() is True
    # The API server and `job-agent-watch` both try; the loser stays quiet
    # instead of doubling every morning's messages.
    assert second.start() is False
    await first.stop()
    assert second.start() is True
    await second.stop()


async def test_a_disabled_scheduler_never_claims_the_lock(tmp_path):
    clock = Clock(datetime(2026, 8, 5, 9, 30, tzinfo=SHANGHAI))
    pushes = Pushes()
    scheduler = build(tmp_path, clock, pushes, enabled=False)

    assert scheduler.enabled is False
    assert scheduler.start() is False
    assert not scheduler.lock_path.exists()


async def test_status_says_when_the_next_one_is_due(tmp_path):
    clock = Clock(datetime(2026, 8, 5, 10, 0, tzinfo=SHANGHAI))
    scheduler = build(tmp_path, clock, Pushes())

    await scheduler.tick()
    status = scheduler.status()

    assert status["push_at"] == "09:30"
    assert status["last_pushed_date"] == "2026-08-05"
    assert status["next_push_at"].startswith("2026-08-06T09:30")
    assert status["pushes"] == 1
