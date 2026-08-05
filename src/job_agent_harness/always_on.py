"""The always-on shift: agents that keep working between @mentions.

An @mention-only harness looks dead in the dashboard — the RPG town shows a
town of idle sprites and there is no way to tell whether routing works until
someone types a message. This module gives one or more roles a real shift:

* every ``interval`` seconds (plus jitter) the role runs a patrol — a real
  orchestrator run with a real prompt, so it produces real evidence;
* between patrols it emits a heartbeat, so the town can honestly say
  "still on duty, last worked N seconds ago" instead of guessing;
* repeated failures back off exponentially instead of hammering the API;
* a file lock keeps exactly one watcher alive per data directory, so running
  ``job-agent-watch`` next to the API server does not double the bill.

The clock and sleep function are injectable, which is what lets the tests drive
several patrol cycles without waiting real minutes.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import logging
import os
import random
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

from .activity import ActivityStore
from .models import RunRequest


LOGGER = logging.getLogger("job_agent_harness.always_on")

#: Rotated so consecutive patrols do different work instead of re-asking the
#: same question and re-deriving the same answer.
DEFAULT_PATROL_PROMPTS: tuple[str, ...] = (
    "巡检岗位表里还没投递的岗位：抓官方页面，报告是否还开放、"
    "网申截止时间有没有变化，并给出可核对的 JD 原文引用。",
    "在岗位表之外找 1-2 个新的 2027 届 AI/Agent 方向正式校招岗位，"
    "给出公司、岗位全名、官方链接和 JD 原文引用，找不到就说找不到。",
    "复核岗位表里标为已投递的岗位：官方页面上现在的状态是什么，"
    "有没有新的批次或补招信息，用原文引用作证。",
)


def _split_env(name: str, default: str) -> list[str]:
    raw = os.getenv(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


def _is_truthy(value: str | None, default: bool = True) -> bool:
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class SingleInstanceLock:
    """A ``flock`` guard so only one watcher runs per data directory."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._descriptor: int | None = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(descriptor)
            return False
        os.truncate(descriptor, 0)
        os.write(descriptor, str(os.getpid()).encode("ascii"))
        self._descriptor = descriptor
        return True

    def release(self) -> None:
        if self._descriptor is None:
            return
        with contextlib.suppress(OSError):
            fcntl.flock(self._descriptor, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(self._descriptor)
        self._descriptor = None

    @property
    def held(self) -> bool:
        return self._descriptor is not None


class AlwaysOnWatcher:
    """Runs patrol shifts for the roles that should never go idle."""

    def __init__(
        self,
        *,
        orchestrator: Any,
        activity_store: ActivityStore,
        roles: Sequence[str] | None = None,
        prompts: Sequence[str] | None = None,
        interval_seconds: float | None = None,
        jitter_seconds: float | None = None,
        heartbeat_seconds: float | None = None,
        first_delay_seconds: float | None = None,
        max_backoff_seconds: float | None = None,
        timeout_seconds: float | None = None,
        lock_path: Path | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        monotonic: Callable[[], float] | None = None,
        jitter: Callable[[float], float] | None = None,
    ):
        self.orchestrator = orchestrator
        self.activity_store = activity_store
        self.roles = list(
            roles
            if roles is not None
            else _split_env("JOB_AGENT_ALWAYS_ON_ROLES", "job_scout")
        )
        self.prompts = tuple(prompts) if prompts else DEFAULT_PATROL_PROMPTS
        self.interval_seconds = float(
            interval_seconds
            if interval_seconds is not None
            else os.getenv("JOB_AGENT_ALWAYS_ON_INTERVAL_SECONDS", "900")
        )
        self.jitter_seconds = float(
            jitter_seconds
            if jitter_seconds is not None
            else os.getenv("JOB_AGENT_ALWAYS_ON_JITTER_SECONDS", "120")
        )
        self.heartbeat_seconds = float(
            heartbeat_seconds
            if heartbeat_seconds is not None
            else os.getenv("JOB_AGENT_ALWAYS_ON_HEARTBEAT_SECONDS", "30")
        )
        self.first_delay_seconds = float(
            first_delay_seconds
            if first_delay_seconds is not None
            else os.getenv("JOB_AGENT_ALWAYS_ON_FIRST_DELAY_SECONDS", "20")
        )
        self.max_backoff_seconds = float(
            max_backoff_seconds
            if max_backoff_seconds is not None
            else os.getenv("JOB_AGENT_ALWAYS_ON_MAX_BACKOFF_SECONDS", "3600")
        )
        # A patrol answers to nobody, so it gets a far bigger budget than the
        # 45 s an @mention allows: grounding one JD means a search, a fetch and
        # usually two or three tool rounds.
        self.timeout_seconds = float(
            timeout_seconds
            if timeout_seconds is not None
            else os.getenv("JOB_AGENT_ALWAYS_ON_TIMEOUT_SECONDS", "300")
        )
        self.lock_path = Path(lock_path) if lock_path else None
        self._sleep = sleep or asyncio.sleep
        self._monotonic = monotonic or time.monotonic
        self._jitter = jitter or (lambda span: random.uniform(0, span))
        self._lock = SingleInstanceLock(self.lock_path) if self.lock_path else None
        self._task: asyncio.Task[None] | None = None
        self.patrols = 0
        self.failures = 0
        self.last_patrol_at: float | None = None
        self.last_error: str | None = None

    # ------------------------------------------------------------- lifecycle

    @property
    def enabled(self) -> bool:
        return bool(self.roles) and self.interval_seconds > 0

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> bool:
        """Claim the shift. Returns False when another process already has it."""

        if self.running or not self.enabled:
            return False
        if self._lock is not None and not self._lock.acquire():
            LOGGER.info(
                "常驻巡检已在另一个进程运行（锁 %s），本进程跳过。",
                self._lock.path,
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
        if self._lock is not None:
            self._lock.release()

    def status(self) -> dict[str, Any]:
        """What the dashboard needs to say "still on duty" truthfully."""

        idle_for = (
            None
            if self.last_patrol_at is None
            else round(self._monotonic() - self.last_patrol_at, 1)
        )
        return {
            "enabled": self.enabled,
            "running": self.running,
            "roles": list(self.roles),
            "interval_seconds": self.interval_seconds,
            "timeout_seconds": self.timeout_seconds,
            "patrols": self.patrols,
            "consecutive_failures": self.failures,
            "seconds_since_last_patrol": idle_for,
            "last_error": self.last_error,
        }

    # ------------------------------------------------------------------ loop

    def next_delay(self) -> float:
        """Interval plus jitter, doubled per consecutive failure."""

        base = self.interval_seconds * (2 ** min(self.failures, 6))
        return min(base, self.max_backoff_seconds) + self._jitter(
            self.jitter_seconds
        )

    async def loop(self) -> None:
        try:
            await self.wait(self.first_delay_seconds)
            while True:
                for index, role_id in enumerate(self.roles):
                    await self.patrol(role_id, self.patrols + index)
                self.patrols += len(self.roles)
                await self.wait(self.next_delay())
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - the loop must never die quietly
            LOGGER.exception("常驻巡检循环异常退出")
            raise

    async def wait(self, seconds: float) -> None:
        """Sleep, emitting a heartbeat so the town can prove we are alive."""

        remaining = max(0.0, seconds)
        while remaining > 0:
            step = min(self.heartbeat_seconds, remaining) or remaining
            await self._sleep(step)
            remaining -= step
            if remaining > 0:
                self.heartbeat(remaining)

    def heartbeat(self, remaining: float) -> None:
        self.activity_store.record(
            run_id="always-on",
            kind="heartbeat",
            status="idle",
            orchestrator="always_on",
            phase="patrol",
            selected_role_ids=list(self.roles),
            metrics={
                "next_patrol_in_seconds": round(remaining, 1),
                "patrols": self.patrols,
                "consecutive_failures": self.failures,
            },
        )

    def prompt_for(self, index: int) -> str:
        return self.prompts[index % len(self.prompts)]

    async def patrol(self, role_id: str, index: int) -> None:
        prompt = self.prompt_for(index)
        run_id = f"patrol-{uuid.uuid4().hex[:8]}"
        started = self._monotonic()
        self.activity_store.record(
            run_id=run_id,
            kind="patrol_started",
            status="running",
            orchestrator="always_on",
            phase="patrol",
            role_id=role_id,
            query=prompt,
            selected_role_ids=[role_id],
            metrics={"patrol_index": index},
        )
        try:
            report = await self.orchestrator.run(
                RunRequest(
                    query=prompt,
                    requested_roles=[role_id],
                    mode="single",
                    use_judge=False,
                    timeout_seconds=self.timeout_seconds,
                    # Nobody asked for this one. The page reads it to keep the
                    # patrol out of the header while a real task is running.
                    origin="patrol",
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.record_failure(
                run_id=run_id,
                role_id=role_id,
                prompt=prompt,
                started=started,
                error=str(exc),
            )
            return
        # A run can come back "completed" with every agent inside it failed—
        # that is what a gateway timeout looks like from out here. Reporting it
        # as ok would show a green patrol that produced nothing and would keep
        # the backoff from ever kicking in.
        failed = [item for item in report.results if item.status != "ok"]
        if failed or not report.final_output.strip():
            self.record_failure(
                run_id=run_id,
                role_id=role_id,
                prompt=prompt,
                started=started,
                error="; ".join(
                    f"{item.role_id}: {item.status} {item.error or ''}".strip()
                    for item in failed
                )
                or "巡检没有产出任何内容",
                source_run_id=report.run_id,
            )
            return
        self.failures = 0
        self.last_error = None
        self.last_patrol_at = self._monotonic()
        self.activity_store.record(
            run_id=run_id,
            kind="patrol_completed",
            status="ok",
            orchestrator="always_on",
            phase="patrol",
            role_id=role_id,
            latency_ms=report.metrics.wall_latency_ms,
            query=prompt,
            output=report.final_output,
            selected_role_ids=[role_id],
            metrics={
                "source_run_id": report.run_id,
                "model_calls": report.metrics.model_calls,
                "output_tokens": report.metrics.output_tokens,
            },
        )

    def record_failure(
        self,
        *,
        run_id: str,
        role_id: str,
        prompt: str,
        started: float,
        error: str,
        source_run_id: str | None = None,
    ) -> None:
        self.failures += 1
        self.last_error = error
        metrics: dict[str, Any] = {"consecutive_failures": self.failures}
        if source_run_id:
            metrics["source_run_id"] = source_run_id
        self.activity_store.record(
            run_id=run_id,
            kind="patrol_completed",
            status="error",
            orchestrator="always_on",
            phase="patrol",
            role_id=role_id,
            latency_ms=(self._monotonic() - started) * 1000,
            query=prompt,
            error=error,
            selected_role_ids=[role_id],
            metrics=metrics,
        )
        LOGGER.warning("常驻巡检失败（第 %s 次）：%s", self.failures, error)


def build_watcher(orchestrator: Any, activity_store: ActivityStore) -> AlwaysOnWatcher:
    """Environment-configured watcher, sharing the caller's runtime objects."""

    from .runtime import runtime_data_dir

    return AlwaysOnWatcher(
        orchestrator=orchestrator,
        activity_store=activity_store,
        lock_path=runtime_data_dir() / "always_on.lock",
    )


def always_on_enabled() -> bool:
    return _is_truthy(os.getenv("JOB_AGENT_ALWAYS_ON"), True)


async def watch_forever() -> None:
    """Entry point for ``job-agent-watch``."""

    from .daily_schedule import DailyPushScheduler
    from .runtime import build_activity_store, build_orchestrator

    logging.basicConfig(
        level=os.getenv("JOB_AGENT_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    activity_store = build_activity_store()
    watcher = build_watcher(build_orchestrator(), activity_store)
    if not watcher.enabled:
        raise SystemExit(
            "常驻巡检未启用：请设置 JOB_AGENT_ALWAYS_ON_ROLES 和 "
            "JOB_AGENT_ALWAYS_ON_INTERVAL_SECONDS。"
        )
    if not watcher.start():
        raise SystemExit(
            "常驻巡检已在另一个进程运行；不要同时开两个 job-agent-watch。"
        )
    # The daily brief rides along on whichever process is up. Its own lock
    # decides who actually sends, so this and the API server can both run.
    daily_scheduler = DailyPushScheduler()
    if daily_scheduler.start():
        LOGGER.info(
            "每日日报推送已接管：每天 %s（%s）",
            daily_scheduler.push_at.strftime("%H:%M"),
            daily_scheduler.timezone,
        )
    LOGGER.info(
        "常驻巡检已启动：roles=%s interval=%ss",
        ",".join(watcher.roles),
        watcher.interval_seconds,
    )
    try:
        await asyncio.shield(watcher._task)  # type: ignore[arg-type]
    except asyncio.CancelledError:
        pass
    finally:
        await watcher.stop()
        await daily_scheduler.stop()


def main() -> None:
    try:
        asyncio.run(watch_forever())
    except KeyboardInterrupt:
        LOGGER.info("常驻巡检已停止")
