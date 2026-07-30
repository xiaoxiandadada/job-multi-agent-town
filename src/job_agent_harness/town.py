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
    "job_scout": "机会驿站",
    "jd_analyst": "JD 研究所",
    "job_knowledge_curator": "知识图书馆",
    "resume_strategist": "简历工坊",
    "portfolio_coach": "作品车库",
    "interview_coach": "面试竞技场",
    "judge": "证据审判塔",
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
    timeline: list[TownMemory] = Field(default_factory=list)
    replay: TownReplayState = Field(default_factory=TownReplayState)
    task_graph: TaskGraph | None = None


def _event_text(event: ActivityEvent) -> str:
    if event.kind == "run_started":
        return event.query_excerpt or "新任务进入小镇"
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
    current_run_id = events[-1].run_id if events else None
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
        "route" if current_events else "idle",
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
    for role in registry.list_roles(include_disabled=True):
        task = tasks_by_role.get(role.role_id)
        role_events = [
            event
            for event in current_events
            if event.role_id == role.role_id
            and event.kind in {
                "agent_started",
                "agent_completed",
                "daily_push_started",
                "daily_push_completed",
            }
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
            status = (
                "running"
                if last.kind in {"agent_started", "daily_push_started"}
                else last.status
            )
            current_action = (
                f"正在执行 {last.phase} 阶段任务"
                if status == "running"
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
        cognitive_memories = (
            memory_store.list(role_id=role.role_id, limit=memory_limit)
            if memory_store is not None
            else []
        )
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
        timeline=timeline,
        replay=TownReplayState(
            enabled=replay_enabled,
            step=replay_step or 0,
            total_steps=replay_total_steps or 0,
        ),
        task_graph=visible_task_graph,
    )
