from __future__ import annotations

from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from .activity import ActivityEvent
from .cognition import AgentMemory, MemoryStore
from .registry import RoleRegistry
from .tasks import TaskGraph, TaskNode


TOWN_PLACES = {
    "job_scout": "Scout Outpost",
    "jd_analyst": "JD Lab",
    "job_knowledge_curator": "Knowledge Library",
    "resume_strategist": "Resume Workshop",
    "portfolio_coach": "Portfolio Garage",
    "interview_coach": "Interview Arena",
    "judge": "Evidence Court",
}
ROLE_SCHEDULES = {
    "job_scout": ["核验届别与岗位类型", "检查官方来源", "更新机会优先级"],
    "jd_analyst": ["提取硬要求", "区分加分项", "生成技能缺口"],
    "job_knowledge_curator": ["补充领域知识", "整理技术栈", "安排学习优先级"],
    "resume_strategist": ["选择简历版本", "映射项目证据", "改写量化 bullet"],
    "portfolio_coach": ["选择最小作品动作", "定义验收指标", "产出展示材料"],
    "interview_coach": ["生成核心题目", "设计追问", "制定评分 rubric"],
    "judge": ["检查来源", "标记冲突与遗漏", "发布可执行结论"],
}
#: The Plaza is not a role, so routing edges out of the router need a node id
#: that can never collide with a ``role_id``.
PLAZA_NODE = "plaza"
#: A watcher heartbeats every 30 s by default. Four missed beats is long enough
#: to rule out one slow write and short enough that a dead watcher stops
#: claiming to be on duty.
SHIFT_STALE_SECONDS = 120.0
#: Kinds that mean "this role is doing something right now", including the
#: always-on patrol — without these the town shows an idle building while the
#: patrol is provably mid-run.
ROLE_WORK_KINDS = {
    "agent_started",
    "agent_completed",
    "daily_push_started",
    "daily_push_completed",
    "patrol_started",
    "patrol_completed",
}


class TownMemory(BaseModel):
    timestamp: str
    run_id: str
    kind: str
    phase: str
    status: str
    text: str


class TownCognitiveMemory(BaseModel):
    memory_id: str
    timestamp: str
    run_id: str
    kind: str
    text: str
    importance: float


class TownAgentSnapshot(BaseModel):
    role_id: str
    display_name: str
    place: str
    icon: str = "🏠"
    x: float | None = None
    y: float | None = None
    goal: str
    enabled: bool = True
    workflow_stage: str = "auto"
    status: Literal[
        "disabled",
        "idle",
        "queued",
        "running",
        "ok",
        "error",
        "timeout",
    ]
    phase: str = "idle"
    current_action: str
    model: str = "—"
    latency_ms: float | None = None
    schedule: list[str] = Field(default_factory=list)
    memories: list[TownMemory] = Field(default_factory=list)
    memory_stream: list[TownCognitiveMemory] = Field(default_factory=list)
    reflection: str = ""
    task_id: str | None = None
    task_title: str = ""
    task_status: str = ""
    task_progress: int = 0
    depends_on: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)


class TownHandoff(BaseModel):
    timestamp: str
    run_id: str
    source_role_ids: list[str]
    target_role_ids: list[str]
    summary: str


class TownRoute(BaseModel):
    """One directed edge that really carried work between two nodes."""

    timestamp: str
    run_id: str
    kind: Literal["dispatch", "handoff", "review"]
    source_role_id: str
    target_role_id: str
    phase: str
    label: str
    summary: str = ""


class TownShift(BaseModel):
    """What an always-on role's duty log says, as of this snapshot."""

    role_id: str
    display_name: str
    state: Literal["working", "standby", "offline"]
    patrols: int = 0
    consecutive_failures: int = 0
    last_patrol_at: str | None = None
    seconds_since_last_patrol: float | None = None
    next_patrol_in_seconds: float | None = None
    heartbeat_age_seconds: float | None = None
    last_summary: str = ""
    last_error: str | None = None


class TownReplayState(BaseModel):
    enabled: bool = False
    step: int = 0
    total_steps: int = 0


class TownSnapshot(BaseModel):
    generated_at: str
    town_time: str
    current_run_id: str | None = None
    current_phase: str = "idle"
    agents: list[TownAgentSnapshot]
    handoffs: list[TownHandoff] = Field(default_factory=list)
    routes: list[TownRoute] = Field(default_factory=list)
    shifts: list[TownShift] = Field(default_factory=list)
    timeline: list[TownMemory] = Field(default_factory=list)
    replay: TownReplayState = Field(default_factory=TownReplayState)
    task_graph: TaskGraph | None = None


def _event_text(event: ActivityEvent) -> str:
    if event.kind == "run_started":
        return event.query_excerpt or "新任务进入小镇"
    if event.kind == "intake_completed":
        return event.output_excerpt or "Chief of Staff 完成意图判断与分派"
    if event.kind == "attachment_read":
        if event.status != "ok":
            return f"Chief of Staff 读附件失败：{event.error or '未知原因'}"
        return event.output_excerpt or "Chief of Staff 读完用户附件"
    if event.kind == "closing_created":
        if event.status != "ok":
            return f"Chief of Staff 收口失败：{event.error or '未知原因'}"
        return event.output_excerpt or "Chief of Staff 给出下一步建议"
    if event.kind == "route_completed":
        selected = "、".join(event.selected_role_ids)
        return f"路由选择：{selected}" if selected else "路由完成"
    if event.kind == "phase_started":
        return f"{event.phase} 阶段开始"
    if event.kind == "handoff_created":
        sources = "、".join(event.source_role_ids)
        targets = "、".join(event.target_role_ids)
        return f"{sources} → {targets}：共享上游证据"
    if event.kind == "memory_retrieved":
        return event.output_excerpt or "检索长期记忆"
    if event.kind == "plan_updated":
        return event.output_excerpt or "更新本轮计划"
    if event.kind == "reflection_created":
        return event.output_excerpt or "形成阶段反思"
    if event.kind == "task_graph_created":
        return event.output_excerpt or "总控完成任务拆解"
    if event.kind == "task_started":
        return f"开始任务：{event.task_title or event.task_id}"
    if event.kind == "task_completed":
        return (
            event.output_excerpt
            or event.error
            or f"完成任务：{event.task_title or event.task_id}"
        )
    if event.kind == "daily_push_started":
        return f"{event.display_name or event.role_id} 开始推送日报"
    if event.kind == "daily_push_completed":
        return (
            event.output_excerpt
            or event.error
            or f"{event.display_name or event.role_id} 完成日报推送"
        )
    if event.kind == "patrol_started":
        return f"常驻巡检开始：{event.query_excerpt or '例行岗位核验'}"
    if event.kind == "patrol_completed":
        if event.status != "ok":
            return f"常驻巡检失败：{event.error or '未知原因'}"
        return event.output_excerpt or "常驻巡检完成"
    if event.kind == "heartbeat":
        remaining = event.metrics.get("next_patrol_in_seconds")
        if isinstance(remaining, (int, float)):
            return f"在岗待命，下一轮巡检还有 {round(float(remaining))} 秒"
        return "在岗待命"
    if event.kind == "agent_started":
        return f"{event.display_name or event.role_id} 开始工作"
    if event.kind == "agent_completed":
        return (
            event.output_excerpt
            or event.error
            or f"{event.display_name or event.role_id} 完成工作"
        )
    if event.kind == "run_completed":
        return "团队任务完成"
    return event.error or event.output_excerpt or "运行失败"


def _memory(event: ActivityEvent) -> TownMemory:
    return TownMemory(
        timestamp=event.timestamp,
        run_id=event.run_id,
        kind=event.kind,
        phase=event.phase,
        status=event.status,
        text=_event_text(event),
    )


def _cognitive_memory(memory: AgentMemory) -> TownCognitiveMemory:
    return TownCognitiveMemory(
        memory_id=memory.memory_id,
        timestamp=memory.timestamp,
        run_id=memory.run_id,
        kind=memory.kind,
        text=memory.text,
        importance=memory.importance,
    )


def routes_for_events(events: list[ActivityEvent]) -> list[TownRoute]:
    """Every edge that actually carried work, in the order it was used.

    Nothing here is inferred from the graph definition: a dispatch edge exists
    because the router really selected that role, a handoff edge because the
    orchestrator really passed upstream evidence along, a review edge because
    that role's result was already finished when the judge started. An edge the
    page cannot back with an event is an edge the page must not draw.
    """

    routes: dict[tuple[str, str, str], TownRoute] = {}

    def remember(route: TownRoute) -> None:
        # Same edge used twice (a retry, a second phase) keeps the newer stamp.
        routes[(route.kind, route.source_role_id, route.target_role_id)] = route

    completed_before_judge: list[ActivityEvent] = []
    for event in events:
        if event.kind == "route_completed":
            for role_id in event.selected_role_ids:
                remember(
                    TownRoute(
                        timestamp=event.timestamp,
                        run_id=event.run_id,
                        kind="dispatch",
                        source_role_id=PLAZA_NODE,
                        target_role_id=role_id,
                        phase="route",
                        label="Plaza 分派",
                        summary=event.query_excerpt,
                    )
                )
        elif event.kind == "handoff_created":
            for source_id in event.source_role_ids:
                for target_id in event.target_role_ids:
                    if source_id == target_id:
                        continue
                    remember(
                        TownRoute(
                            timestamp=event.timestamp,
                            run_id=event.run_id,
                            kind="handoff",
                            source_role_id=source_id,
                            target_role_id=target_id,
                            phase=event.phase,
                            label="证据交接",
                            summary=event.output_excerpt,
                        )
                    )
        elif event.kind == "agent_completed" and event.role_id != "judge":
            completed_before_judge.append(event)
        elif event.kind == "agent_started" and event.role_id == "judge":
            for done in completed_before_judge:
                if not done.role_id:
                    continue
                remember(
                    TownRoute(
                        timestamp=event.timestamp,
                        run_id=event.run_id,
                        kind="review",
                        source_role_id=done.role_id,
                        target_role_id="judge",
                        phase=event.phase,
                        label="送审",
                        summary=done.output_excerpt,
                    )
                )
    return sorted(routes.values(), key=lambda route: route.timestamp)


def shifts_for_events(
    events: list[ActivityEvent],
    *,
    display_names: dict[str, str],
    now: datetime,
) -> list[TownShift]:
    """Turn the patrol/heartbeat trail into a per-role duty report.

    Reading this from the event log rather than from ``AlwaysOnWatcher.status()``
    is deliberate: the watcher usually lives in the ``job-agent-watch`` process,
    where the API's own in-memory status object knows nothing about it.
    """

    latest: dict[str, ActivityEvent] = {}
    started: dict[str, ActivityEvent] = {}
    completed: dict[str, ActivityEvent] = {}
    heartbeats: dict[str, ActivityEvent] = {}
    patrol_counts: dict[str, int] = {}
    for event in events:
        if event.kind == "heartbeat":
            for role_id in event.selected_role_ids:
                latest[role_id] = event
                heartbeats[role_id] = event
            continue
        if event.kind not in {"patrol_started", "patrol_completed"}:
            continue
        role_id = event.role_id or ""
        if not role_id:
            continue
        latest[role_id] = event
        if event.kind == "patrol_started":
            started[role_id] = event
        else:
            completed[role_id] = event
            patrol_counts[role_id] = patrol_counts.get(role_id, 0) + 1

    shifts: list[TownShift] = []
    for role_id, newest in latest.items():
        last_start = started.get(role_id)
        last_done = completed.get(role_id)
        working = last_start is not None and (
            last_done is None or last_done.timestamp < last_start.timestamp
        )
        age = _age_seconds(newest.timestamp, now)
        if working:
            state: Literal["working", "standby", "offline"] = "working"
        elif age is None or age > SHIFT_STALE_SECONDS:
            state = "offline"
        else:
            state = "standby"
        heartbeat = heartbeats.get(role_id)
        remaining = (
            heartbeat.metrics.get("next_patrol_in_seconds")
            if heartbeat is not None
            else None
        )
        failures = (
            last_done.metrics.get("consecutive_failures")
            if last_done is not None and last_done.status != "ok"
            else 0
        )
        shifts.append(
            TownShift(
                role_id=role_id,
                display_name=display_names.get(role_id, role_id),
                state=state,
                patrols=patrol_counts.get(role_id, 0),
                consecutive_failures=(
                    int(failures) if isinstance(failures, (int, float)) else 0
                ),
                last_patrol_at=(
                    last_done.timestamp if last_done is not None else None
                ),
                seconds_since_last_patrol=(
                    _age_seconds(last_done.timestamp, now)
                    if last_done is not None
                    else None
                ),
                next_patrol_in_seconds=(
                    float(remaining)
                    if isinstance(remaining, (int, float))
                    else None
                ),
                heartbeat_age_seconds=(
                    _age_seconds(heartbeat.timestamp, now)
                    if heartbeat is not None
                    else None
                ),
                last_summary=_event_text(last_start if working else newest),
                last_error=(
                    last_done.error
                    if last_done is not None and last_done.status != "ok"
                    else None
                ),
            )
        )
    return sorted(shifts, key=lambda shift: shift.role_id)


def _age_seconds(timestamp: str, now: datetime) -> float | None:
    try:
        moment = datetime.fromisoformat(timestamp)
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    return round((now - moment).total_seconds(), 1)


def background_run_ids(events: list[ActivityEvent]) -> set[str]:
    """Runs nobody is waiting for: the watcher's own shift and what it dispatched.

    Background work hands an *ordinary* run to the orchestrator, so only that
    run's ``run_started`` carries the origin mark — every later event of it looks
    like any other run and has to be matched by id. Two kinds arrive this way: a
    patrol every few minutes, and the roles writing the daily brief when nobody
    prepared one that morning.
    """

    ids: set[str] = set()
    for event in events:
        metrics = event.metrics or {}
        if (
            event.orchestrator == "always_on"
            or event.kind.startswith("patrol_")
            or metrics.get("origin") in {"patrol", "schedule"}
        ):
            ids.add(event.run_id)
        source = metrics.get("source_run_id")
        if event.kind == "patrol_completed" and isinstance(source, str):
            ids.add(source)
    return ids


def current_run_id_for_events(events: list[ActivityEvent]) -> str | None:
    """The newest run somebody is actually waiting for.

    A heartbeat is not a run — it is the watcher proving it is alive. Letting it
    become the "current run" every 30 s would blank the town between real runs,
    which is exactly the opposite of showing the agents at work.

    Background runs are not what anyone is watching either. A patrol fires every
    few minutes and dispatches a single role, so taking the newest run would rip
    the town and the task board away from a collaborative run halfway through —
    completed tasks dropping from 2/7 back to 1/1. Such a run gets the stage only
    when there is no real run to show, and then the watcher's own shift is what
    we show, because its ``patrol`` phase is what tells the routing panel to
    explain itself.
    """

    background = background_run_ids(events)
    requested = next(
        (
            event.run_id
            for event in reversed(events)
            if event.kind != "heartbeat" and event.run_id not in background
        ),
        None,
    )
    if requested is not None:
        return requested
    return next(
        (
            event.run_id
            for event in reversed(events)
            if event.kind.startswith("patrol_")
        ),
        next(
            (event.run_id for event in reversed(events) if event.kind != "heartbeat"),
            None,
        ),
    )


def task_graph_for_events(
    graph: TaskGraph | None,
    events: list[ActivityEvent],
    *,
    replay: bool,
) -> TaskGraph | None:
    if graph is None or not replay:
        return graph
    projected = graph.model_copy(deep=True)
    by_task = {task.task_id: task for task in projected.tasks}
    for task in projected.tasks:
        task.status = "blocked" if task.depends_on else "ready"
        task.progress = 0
        task.model = "pending"
        task.output_excerpt = ""
        task.error = None
        task.started_at = None
        task.completed_at = None
    for event in events:
        if not event.task_id or event.task_id not in by_task:
            continue
        task = by_task[event.task_id]
        if event.kind == "task_started":
            task.status = "running"
            task.progress = event.progress or 50
            task.model = event.model or task.model
            task.started_at = event.timestamp
        elif event.kind == "task_completed":
            task.status = (
                "completed"
                if event.status in {"ok", "completed"}
                else event.status
            )
            task.progress = 100
            task.model = event.model or task.model
            task.output_excerpt = event.output_excerpt
            task.error = event.error
            task.completed_at = event.timestamp
        for candidate in projected.tasks:
            if candidate.status not in {"blocked", "ready"}:
                continue
            dependencies = [
                by_task[item]
                for item in candidate.depends_on
                if item in by_task
            ]
            candidate.status = (
                "ready"
                if not dependencies
                or all(item.status == "completed" for item in dependencies)
                else "blocked"
            )
    projected.progress = (
        round(
            sum(task.progress for task in projected.tasks)
            / len(projected.tasks)
        )
        if projected.tasks
        else 100
    )
    statuses = {task.status for task in projected.tasks}
    if not projected.tasks or statuses == {"completed"}:
        projected.status = "completed"
    elif statuses & {"error", "timeout"}:
        projected.status = "partial"
    elif statuses & {"running", "completed"}:
        projected.status = "running"
    else:
        projected.status = "queued"
    return projected


def build_town_snapshot(
    registry: RoleRegistry,
    events: list[ActivityEvent],
    *,
    memory_store: MemoryStore | None = None,
    memory_limit: int = 6,
    timeline_limit: int = 18,
    replay_step: int | None = None,
    replay_total_steps: int | None = None,
    task_graph: TaskGraph | None = None,
) -> TownSnapshot:
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    replay_enabled = replay_total_steps is not None
    visible_task_graph = task_graph_for_events(
        task_graph,
        events,
        replay=replay_enabled,
    )
    tasks_by_role: dict[str, TaskNode] = (
        {
            task.role_id: task
            for task in visible_task_graph.tasks
        }
        if visible_task_graph is not None
        else {}
    )
    display_time = now
    if replay_enabled and events:
        display_time = datetime.fromisoformat(
            events[-1].timestamp
        ).astimezone(ZoneInfo("Asia/Shanghai"))
    current_run_id = current_run_id_for_events(events)
    current_events = (
        [event for event in events if event.run_id == current_run_id]
        if current_run_id
        else []
    )
    run_finished = any(
        event.kind in {"run_completed", "run_failed"}
        for event in current_events
    )
    latest_phase = next(
        (
            event.phase
            for event in reversed(current_events)
            if event.kind == "phase_started"
        ),
        (
            "patrol"
            if any(
                event.kind in {"patrol_started", "patrol_completed"}
                for event in current_events
            )
            else "route"
            if current_events
            else "idle"
        ),
    )
    if run_finished:
        latest_phase = "completed"

    selected_ids: set[str] = set()
    for event in current_events:
        if event.kind == "route_completed":
            selected_ids.update(event.selected_role_ids)
        if event.kind == "phase_started":
            selected_ids.update(event.selected_role_ids)

    agents: list[TownAgentSnapshot] = []
    # One pass over memories.jsonl for all roles. Asking per role meant parsing
    # the same file seven times per snapshot, and the dashboard polls every 1.5s.
    memories_by_role = (
        memory_store.list_by_role(limit=memory_limit)
        if memory_store is not None
        else {}
    )
    for role in registry.list_roles(include_disabled=True):
        task = tasks_by_role.get(role.role_id)
        role_events = [
            event
            for event in current_events
            if event.role_id == role.role_id
            and event.kind in ROLE_WORK_KINDS
        ]
        last = role_events[-1] if role_events else None
        if not role.enabled:
            status = "disabled"
            current_action = "角色已暂停，不参与自动路由"
            phase = "idle"
            model = "—"
            latency_ms = None
        elif last is None:
            status = (
                "queued"
                if role.role_id in selected_ids
                or (
                    task is not None
                    and task.status in {"blocked", "ready"}
                )
                else "idle"
            )
            current_action = (
                (
                    f"等待依赖：{'、'.join(task.depends_on)}"
                    if task is not None
                    and task.status == "blocked"
                    else task.description
                    if task is not None
                    else "在 LangGraph Plaza 等待调度"
                )
                if status == "queued"
                else "在自己的建筑待命"
            )
            phase = task.phase if task is not None else (
                "route" if status == "queued" else "idle"
            )
            model = task.model if task is not None else "—"
            latency_ms = None
        else:
            running = last.kind in {
                "agent_started",
                "daily_push_started",
                "patrol_started",
            }
            status = "running" if running else last.status
            current_action = (
                "常驻巡检中：核验真实岗位与链接"
                if last.kind == "patrol_started"
                else f"正在执行 {last.phase} 阶段任务"
                if running
                else _event_text(last)
            )
            phase = last.phase
            model = last.model or "pending"
            latency_ms = last.latency_ms

        related_events = [
            event
            for event in events
            if event.role_id == role.role_id
            or role.role_id in event.source_role_ids
            or role.role_id in event.target_role_ids
        ]
        cognitive_memories = memories_by_role.get(role.role_id, [])
        latest_reflection = next(
            (
                memory.text
                for memory in reversed(cognitive_memories)
                if memory.kind == "reflection"
            ),
            "",
        )
        agents.append(
            TownAgentSnapshot(
                role_id=role.role_id,
                display_name=role.display_name,
                place=(
                    role.town_place
                    or TOWN_PLACES.get(role.role_id, role.display_name)
                ),
                icon=role.town_icon,
                x=role.town_x,
                y=role.town_y,
                goal=role.goal,
                enabled=role.enabled,
                workflow_stage=role.workflow_stage,
                status=status,
                phase=phase,
                current_action=current_action,
                model=model,
                latency_ms=latency_ms,
                schedule=(
                    role.schedule
                    or ROLE_SCHEDULES.get(
                        role.role_id,
                        ["接收任务", "执行角色目标", "提交可核验结果"],
                    )
                ),
                memories=[
                    _memory(event)
                    for event in related_events[-max(1, memory_limit) :]
                ],
                memory_stream=[
                    _cognitive_memory(memory)
                    for memory in cognitive_memories
                ],
                reflection=latest_reflection,
                task_id=task.task_id if task is not None else None,
                task_title=task.title if task is not None else "",
                task_status=task.status if task is not None else "",
                task_progress=task.progress if task is not None else 0,
                depends_on=task.depends_on if task is not None else [],
                acceptance_criteria=(
                    task.acceptance_criteria if task is not None else []
                ),
            )
        )

    handoffs = [
        TownHandoff(
            timestamp=event.timestamp,
            run_id=event.run_id,
            source_role_ids=event.source_role_ids,
            target_role_ids=event.target_role_ids,
            summary=event.output_excerpt,
        )
        for event in events
        if event.kind == "handoff_created"
    ][-8:]
    timeline = [
        _memory(event)
        for event in events[-max(1, timeline_limit) :]
    ]
    return TownSnapshot(
        generated_at=now.isoformat(),
        town_time=display_time.strftime("%Y-%m-%d %H:%M:%S"),
        current_run_id=current_run_id,
        current_phase=latest_phase,
        agents=agents,
        handoffs=handoffs,
        routes=routes_for_events(current_events),
        shifts=shifts_for_events(
            events,
            display_names={
                agent.role_id: agent.display_name for agent in agents
            },
            now=display_time,
        ),
        timeline=timeline,
        replay=TownReplayState(
            enabled=replay_enabled,
            step=replay_step or 0,
            total_steps=replay_total_steps or 0,
        ),
        task_graph=visible_task_graph,
    )
