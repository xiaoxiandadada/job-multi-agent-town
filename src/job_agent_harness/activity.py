from __future__ import annotations

import json
import os
import threading
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field


ActivityKind = Literal[
    "run_started",
    "intake_completed",
    "attachment_read",
    "route_completed",
    "phase_started",
    "handoff_created",
    "memory_retrieved",
    "plan_updated",
    "reflection_created",
    "task_graph_created",
    "task_started",
    "task_completed",
    "daily_push_started",
    "daily_push_completed",
    "patrol_started",
    "patrol_completed",
    "heartbeat",
    "agent_started",
    "agent_completed",
    "closing_created",
    "run_completed",
    "run_failed",
]
ActivityStatus = Literal[
    "queued",
    "running",
    "ok",
    "error",
    "timeout",
    "completed",
    "idle",
]


class ActivityEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    run_id: str
    kind: ActivityKind
    status: ActivityStatus
    orchestrator: str
    phase: str = "system"
    mode: str = ""
    role_id: str | None = None
    display_name: str | None = None
    model: str | None = None
    latency_ms: float | None = None
    query_excerpt: str = ""
    output_excerpt: str = ""
    error: str | None = None
    selected_role_ids: list[str] = Field(default_factory=list)
    source_role_ids: list[str] = Field(default_factory=list)
    target_role_ids: list[str] = Field(default_factory=list)
    task_id: str | None = None
    task_title: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    progress: int | None = Field(default=None, ge=0, le=100)
    metrics: dict[str, Any] = Field(default_factory=dict)


def _reversed_lines(
    path: Path,
    *,
    chunk_size: int = 1 << 16,
) -> Iterator[str]:
    """Yield a file's non-empty lines last-to-first, reading lazily.

    Binary mode and a manual seek rather than ``readlines()`` because the point is
    to never touch the head of the file. Bytes are decoded per line, so a torn
    write from another process costs one unparseable line instead of an exception
    for the whole read.
    """

    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        position = handle.tell()
        pending = b""
        while position > 0:
            size = min(chunk_size, position)
            position -= size
            handle.seek(position)
            block = handle.read(size) + pending
            lines = block.split(b"\n")
            # The first element may be the tail of a line that starts in the
            # chunk we have not read yet, so it has to wait for the next block.
            pending = lines[0]
            for line in reversed(lines[1:]):
                if line.strip():
                    yield line.decode("utf-8", errors="replace")
        if pending.strip():
            yield pending.decode("utf-8", errors="replace")


class ActivityStore:
    """Append-only, cross-process run trace used by the live dashboard."""

    def __init__(self, path: Path, excerpt_chars: int = 420):
        self.path = path
        self.excerpt_chars = max(80, excerpt_chars)
        self._lock = threading.Lock()

    def excerpt(self, value: str) -> str:
        normalized = " ".join(value.split())
        if len(normalized) <= self.excerpt_chars:
            return normalized
        return normalized[: self.excerpt_chars - 1] + "…"

    def emit(self, event: ActivityEvent) -> ActivityEvent:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = (
            json.dumps(
                event.model_dump(),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        with self._lock:
            descriptor = os.open(
                self.path,
                os.O_APPEND | os.O_CREAT | os.O_WRONLY,
                0o600,
            )
            try:
                os.write(descriptor, payload)
            finally:
                os.close(descriptor)
        return event

    def record(
        self,
        *,
        run_id: str,
        kind: ActivityKind,
        status: ActivityStatus,
        orchestrator: str,
        phase: str = "system",
        mode: str = "",
        role_id: str | None = None,
        display_name: str | None = None,
        model: str | None = None,
        latency_ms: float | None = None,
        query: str = "",
        output: str = "",
        error: str | None = None,
        selected_role_ids: list[str] | None = None,
        source_role_ids: list[str] | None = None,
        target_role_ids: list[str] | None = None,
        task_id: str | None = None,
        task_title: str | None = None,
        depends_on: list[str] | None = None,
        progress: int | None = None,
        metrics: dict[str, Any] | None = None,
    ) -> ActivityEvent:
        return self.emit(
            ActivityEvent(
                run_id=run_id,
                kind=kind,
                status=status,
                orchestrator=orchestrator,
                phase=phase,
                mode=mode,
                role_id=role_id,
                display_name=display_name,
                model=model,
                latency_ms=latency_ms,
                query_excerpt=self.excerpt(query) if query else "",
                output_excerpt=self.excerpt(output) if output else "",
                error=self.excerpt(error) if error else None,
                selected_role_ids=selected_role_ids or [],
                source_role_ids=source_role_ids or [],
                target_role_ids=target_role_ids or [],
                task_id=task_id,
                task_title=task_title,
                depends_on=depends_on or [],
                progress=progress,
                metrics=metrics or {},
            )
        )

    def read(
        self,
        *,
        limit: int = 300,
        run_id: str | None = None,
    ) -> list[ActivityEvent]:
        """The last ``limit`` events, oldest first.

        Reads backwards from the end of the file and stops as soon as it has
        enough. The obvious implementation — read the whole file, validate every
        line, keep the tail — costs the same on an empty page as on a busy one and
        grows forever: the web page polls this every 1.5 seconds, two thirds of the
        file is always-on heartbeats, and nothing ever deletes a line. Scanning
        from the end makes the cost proportional to what is asked for instead of to
        how long the process has been running.
        """

        if not self.path.exists():
            return []
        wanted = max(1, min(limit, 2000))
        events: list[ActivityEvent] = []
        for line in _reversed_lines(self.path):
            try:
                event = ActivityEvent.model_validate_json(line)
            except (ValueError, json.JSONDecodeError):
                continue
            if run_id is not None and event.run_id != run_id:
                continue
            events.append(event)
            if len(events) >= wanted:
                break
        events.reverse()
        return events
