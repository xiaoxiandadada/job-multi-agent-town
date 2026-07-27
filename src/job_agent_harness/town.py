from __future__ import annotations

from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from .activity import ActivityEvent
from .registry import RoleRegistry


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


class TownAgentSnapshot(BaseModel):
    role_id: str
    display_name: str
    place: str
    goal: str
    status: Literal[
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


class TownHandoff(BaseModel):
    timestamp: str
    run_id: str
    source_role_ids: list[str]
    target_role_ids: list[str]
    summary: str


class TownSnapshot(BaseModel):
    generated_at: str
    town_time: str
    current_run_id: str | None = None
    current_phase: str = "idle"
    agents: list[TownAgentSnapshot]
    handoffs: list[TownHandoff] = Field(default_factory=list)
    timeline: list[TownMemory] = Field(default_factory=list)


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


def build_town_snapshot(
    registry: RoleRegistry,
    events: list[ActivityEvent],
    *,
    memory_limit: int = 6,
    timeline_limit: int = 18,
) -> TownSnapshot:
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
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
        role_events = [
            event
            for event in current_events
            if event.role_id == role.role_id
        ]
        last = role_events[-1] if role_events else None
        if last is None:
            status = "queued" if role.role_id in selected_ids else "idle"
            current_action = (
                "在 LangGraph Plaza 等待调度"
                if status == "queued"
                else "在自己的建筑待命"
            )
            phase = "route" if status == "queued" else "idle"
            model = "—"
            latency_ms = None
        else:
            status = (
                "running" if last.kind == "agent_started" else last.status
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
        agents.append(
            TownAgentSnapshot(
                role_id=role.role_id,
                display_name=role.display_name,
                place=TOWN_PLACES.get(role.role_id, role.display_name),
                goal=role.goal,
                status=status,
                phase=phase,
                current_action=current_action,
                model=model,
                latency_ms=latency_ms,
                schedule=ROLE_SCHEDULES.get(
                    role.role_id,
                    ["接收任务", "执行角色目标", "提交可核验结果"],
                ),
                memories=[
                    _memory(event)
                    for event in related_events[-max(1, memory_limit) :]
                ],
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
        town_time=now.strftime("%Y-%m-%d %H:%M:%S"),
        current_run_id=current_run_id,
        current_phase=latest_phase,
        agents=agents,
        handoffs=handoffs,
        timeline=timeline,
    )
