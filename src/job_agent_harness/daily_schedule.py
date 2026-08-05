"""Once a morning, on its own, without double-pushing.

``job-agent-push-daily`` existed but nothing ever called it — there was no cron
entry, no launchd job, and the always-on watcher only patrols. So "every day"
was a command the user had to remember to type.

This scheduler is a *poller*, not a sleeper, and that is the whole design:

* a laptop lid closes at 02:00 and opens at 11:00, so a task that slept until
  exactly 09:30 would simply never wake for that slot. Polling asks "is it past
  today's slot and has today been pushed?" instead, which makes waking up late
  the normal case rather than a missed day;
* how late is still useful is bounded (``max_delay_hours``) — a 09:30 brief
  delivered at 23:00 is noise, not a reminder;
* what was already pushed is on disk, so a restart at 09:31 does not push twice;
* a ``flock`` keeps the API server and ``job-agent-watch`` from both pushing,
  the same way the patrol lock already does.

The clock and the sleep are injectable so tests can drive a whole week without
waiting one.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tempfile
from collections.abc import Awaitable, Callable
from datetime import datetime, time as clock_time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .always_on import SingleInstanceLock, _is_truthy


LOGGER = logging.getLogger("job_agent_harness.daily_schedule")

#: The hand-written brief lands around 09:05 most days, so 09:30 leaves it a
#: margin instead of racing it.
DEFAULT_PUSH_AT = "09:30"
DEFAULT_TIMEZONE = "Asia/Shanghai"
STATE_FILENAME = "daily_push_state.json"


def parse_clock(raw: str) -> clock_time:
    """``"09:30"`` → ``time(9, 30)``, with a message that names the bad value."""

    parts = raw.strip().split(":")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        raise ValueError(f"推送时间必须写成 HH:MM，收到的是 {raw!r}")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"推送时间超出范围：{raw!r}")
    return clock_time(hour=hour, minute=minute)


PushCallable = Callable[..., Awaitable[Any]]


class DailyPushScheduler:
    """Fires ``push_daily`` once per local day, catching up after a sleep."""

    def __init__(
        self,
        *,
        push: PushCallable | None = None,
        data_dir: Path | None = None,
        push_at: str | clock_time | None = None,
        timezone: str | None = None,
        max_delay_hours: float | None = None,
        full: bool | None = None,
        poll_seconds: float | None = None,
        retry_seconds: float | None = None,
        enabled: bool | None = None,
        lock_path: Path | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        now: Callable[[], datetime] | None = None,
    ):
        self._push = push
        self.data_dir = Path(
            data_dir if data_dir is not None else _runtime_data_dir()
        )
        raw_at = (
            push_at
            if push_at is not None
            else os.getenv("JOB_AGENT_DAILY_PUSH_AT", DEFAULT_PUSH_AT)
        )
        self.push_at = (
            raw_at if isinstance(raw_at, clock_time) else parse_clock(raw_at)
        )
        self.timezone = ZoneInfo(
            timezone
            if timezone is not None
            else os.getenv("JOB_AGENT_DAILY_PUSH_TZ", DEFAULT_TIMEZONE)
        )
        self.max_delay_hours = float(
            max_delay_hours
            if max_delay_hours is not None
            else os.getenv("JOB_AGENT_DAILY_PUSH_MAX_DELAY_HOURS", "6")
        )
        self.full = (
            full
            if full is not None
            else _is_truthy(os.getenv("JOB_AGENT_DAILY_PUSH_FULL"), False)
        )
        self.poll_seconds = float(
            poll_seconds
            if poll_seconds is not None
            else os.getenv("JOB_AGENT_DAILY_PUSH_POLL_SECONDS", "120")
        )
        # A failed push retries inside the catch-up window, but not every poll:
        # if Feishu is down, hammering it every two minutes just fills the log.
        self.retry_seconds = float(
            retry_seconds
            if retry_seconds is not None
            else os.getenv("JOB_AGENT_DAILY_PUSH_RETRY_SECONDS", "600")
        )
        self._enabled = (
            enabled
            if enabled is not None
            else _is_truthy(os.getenv("JOB_AGENT_DAILY_PUSH"), True)
        )
        self.lock_path = (
            Path(lock_path)
            if lock_path is not None
            else self.data_dir / "daily_push.lock"
        )
        self._sleep = sleep or asyncio.sleep
        self._now = now or (lambda: datetime.now(self.timezone))
        self._lock = SingleInstanceLock(self.lock_path)
        self._task: asyncio.Task[None] | None = None
        #: Said once per day, not once per poll.
        self._late_logged: str | None = None
        self._retry_after: datetime | None = None
        self.pushes = 0
        self.failures = 0
        self.last_error: str | None = None

    # ------------------------------------------------------------- lifecycle

    @property
    def enabled(self) -> bool:
        return self._enabled and self.poll_seconds > 0

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> bool:
        """Claim the job. False when it is disabled or another process has it."""

        if self.running or not self.enabled:
            return False
        if not self._lock.acquire():
            LOGGER.info(
                "每日推送已在另一个进程负责（锁 %s），本进程跳过。",
                self.lock_path,
            )
            return False
        self._task = asyncio.get_running_loop().create_task(self.loop())
        return True

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._lock.release()

    def status(self) -> dict[str, Any]:
        now = self._now()
        return {
            "enabled": self.enabled,
            "running": self.running,
            "push_at": self.push_at.strftime("%H:%M"),
            "timezone": str(self.timezone),
            "full": self.full,
            "max_delay_hours": self.max_delay_hours,
            "last_pushed_date": self.last_pushed_date(),
            "next_push_at": self.next_slot(now).isoformat(),
            "pushes": self.pushes,
            "consecutive_failures": self.failures,
            "last_error": self.last_error,
        }

    # ----------------------------------------------------------------- state

    @property
    def state_path(self) -> Path:
        return self.data_dir / STATE_FILENAME

    def read_state(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def last_pushed_date(self) -> str | None:
        value = self.read_state().get("last_pushed_date")
        return value if isinstance(value, str) else None

    def remember_pushed(self, target_date: str, *, at: datetime) -> None:
        """Write the receipt before anything else can crash.

        This is what makes a restart safe: the state file, not process memory,
        is the answer to "did today already go out?".
        """

        self.data_dir.mkdir(parents=True, exist_ok=True)
        payload = (
            json.dumps(
                {
                    "last_pushed_date": target_date,
                    "last_pushed_at": at.isoformat(),
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        fd, temp_name = tempfile.mkstemp(
            dir=self.data_dir,
            prefix=".daily-push-state.",
            text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.replace(temp_name, self.state_path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    # ------------------------------------------------------------------ clock

    def slot_for(self, moment: datetime) -> datetime:
        return datetime.combine(
            moment.astimezone(self.timezone).date(),
            self.push_at,
            tzinfo=self.timezone,
        )

    def next_slot(self, now: datetime) -> datetime:
        today = self.slot_for(now)
        return today if now < today else today + timedelta(days=1)

    def due_date(self, now: datetime) -> str | None:
        """The date to push, or ``None`` when there is nothing to do yet.

        Four ways to get ``None``, and they are all deliberate: before the slot,
        after the catch-up window closed, already pushed today, and backing off
        from a failure.
        """

        local = now.astimezone(self.timezone)
        slot = self.slot_for(local)
        if local < slot:
            return None
        target_date = local.date().isoformat()
        if self.last_pushed_date() == target_date:
            return None
        if local - slot > timedelta(hours=self.max_delay_hours):
            if self._late_logged != target_date:
                self._late_logged = target_date
                LOGGER.warning(
                    "%s 的日报已经错过 %s 小时的补推窗口，今天不再推送。",
                    target_date,
                    self.max_delay_hours,
                )
            return None
        if self._retry_after is not None and local < self._retry_after:
            return None
        return target_date

    # ------------------------------------------------------------------- work

    async def push(self, target_date: str) -> None:
        if self._push is not None:
            await self._push(target_date, self.full)
            return
        from .push_daily import push_daily

        await push_daily(target_date, self.full, ensure_brief=True)

    async def tick(self) -> str | None:
        """One poll. Returns the date it pushed, or ``None``."""

        now = self._now()
        target_date = self.due_date(now)
        if target_date is None:
            return None
        try:
            await self.push(target_date)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.failures += 1
            self.last_error = str(exc)
            # Not remembered as pushed, so the next poll after the backoff tries
            # again — as long as the catch-up window is still open.
            self._retry_after = now.astimezone(self.timezone) + timedelta(
                seconds=self.retry_seconds
            )
            LOGGER.warning(
                "%s 的日报推送失败（第 %s 次），%.0f 秒后重试：%s",
                target_date,
                self.failures,
                self.retry_seconds,
                exc,
            )
            return None
        self.pushes += 1
        self.failures = 0
        self.last_error = None
        self._retry_after = None
        self.remember_pushed(target_date, at=now)
        LOGGER.info("%s 的日报已推送。", target_date)
        return target_date

    async def loop(self) -> None:
        try:
            while True:
                await self.tick()
                await self._sleep(self.poll_seconds)
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - the loop must not die quietly
            LOGGER.exception("每日推送循环异常退出")
            raise


def _runtime_data_dir() -> Path:
    from .runtime import runtime_data_dir

    return runtime_data_dir()


def build_scheduler(**overrides: Any) -> DailyPushScheduler:
    return DailyPushScheduler(**overrides)


def daily_push_enabled() -> bool:
    return _is_truthy(os.getenv("JOB_AGENT_DAILY_PUSH"), True)
