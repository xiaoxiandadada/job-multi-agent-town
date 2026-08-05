from __future__ import annotations

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from .models import RoleSpec


TaskPhase = Literal["discovery", "analysis", "action", "judge", "delivery"]
TaskStatus = Literal[
    "blocked",
    "ready",
    "running",
    "completed",
    "error",
    "timeout",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TaskNode(BaseModel):
    task_id: str
    title: str
    description: str
    role_id: str
    display_name: str
    phase: TaskPhase
    status: TaskStatus = "blocked"
    progress: int = Field(default=0, ge=0, le=100)
    depends_on: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    model: str = "pending"
    output_excerpt: str = ""
    error: str | None = None
    started_at: str | None = None
    completed_at: str | None = None


class TaskGraph(BaseModel):
    graph_id: str
    run_id: str
    title: str
    query: str
    source: str
    mode: str
    status: Literal["queued", "running", "completed", "partial", "error"]
    progress: int = Field(default=0, ge=0, le=100)
    created_at: str
    updated_at: str
    tasks: list[TaskNode]


ROLE_ACCEPTANCE_CRITERIA = {
    "job_scout": [
        "只保留中国 2027 届正式校招",
        "给出可访问的官方 JD 来源",
        "标记截止时间和待核验风险",
    ],
    "jd_analyst": [
        "区分硬要求与加分项",
        "提取关键词和技能缺口",
        "所有判断可回指 JD 原文",
    ],
    "job_knowledge_curator": [
        "覆盖核心概念、技术栈和工程难点",
        "给出面试问题与学习优先级",
        "区分通用知识和公司特定推断",
    ],
    "resume_strategist": [
        "选择明确的简历版本",
        "bullet 包含问题、方法、结果和指标",
        "不虚构经历或量化结果",
    ],
    "portfolio_coach": [
        "给出最小可验证作品动作",
        "包含样例数据、技术栈和验收指标",
        "能形成 GitHub 可展示证据",
    ],
    "interview_coach": [
        "问题映射到 JD 与项目证据",
        "包含追问和评分 rubric",
        "给出当天可执行的训练产出",
    ],
    "judge": [
        "删除无来源或冲突陈述",
        "标出遗漏、风险和待核验项",
        "输出有优先级的最终行动清单",
    ],
}


def role_phase(role: RoleSpec, mode: str) -> TaskPhase:
    if role.role_id == "judge":
        return "judge"
    if mode != "collaborative":
        return "action"
    if role.role_id == "job_scout":
        return "discovery"
    if role.workflow_stage == "context" or role.role_id in {
        "jd_analyst",
        "job_knowledge_curator",
    }:
        return "analysis"
    return "action"


def build_run_task_graph(
    *,
    run_id: str,
    query: str,
    roles: list[RoleSpec],
    mode: str,
    use_judge: bool,
) -> TaskGraph:
    work_roles = [role for role in roles if role.role_id != "judge"]
    phases = {role.role_id: role_phase(role, mode) for role in work_roles}
    discovery_ids = [
        role.role_id
        for role in work_roles
        if phases[role.role_id] == "discovery"
    ]
    analysis_ids = [
        role.role_id
        for role in work_roles
        if phases[role.role_id] == "analysis"
    ]
    upstream_ids = [*discovery_ids, *analysis_ids]
    tasks: list[TaskNode] = []
    for role in work_roles:
        phase = phases[role.role_id]
        if phase == "analysis":
            dependencies = discovery_ids
        elif phase == "action":
            dependencies = upstream_ids
        else:
            dependencies = []
        tasks.append(
            TaskNode(
                task_id=role.role_id,
                title=f"{role.display_name}：{role.goal}",
                description=(
                    f"围绕本轮任务完成角色交付：{query[:1200]}"
                ),
                role_id=role.role_id,
                display_name=role.display_name,
                phase=phase,
                status="ready" if not dependencies else "blocked",
                depends_on=dependencies,
                acceptance_criteria=ROLE_ACCEPTANCE_CRITERIA.get(
                    role.role_id,
                    ["完成角色目标", "给出可复核证据", "明确待核验内容"],
                ),
            )
        )
    if use_judge:
        tasks.append(
            TaskNode(
                task_id="judge",
                title="Evidence Judge：审核并发布最终结果",
                description="汇总所有角色交付，检查来源、冲突、遗漏和可执行性。",
                role_id="judge",
                display_name="Evidence Judge",
                phase="judge",
                status="ready" if not tasks else "blocked",
                depends_on=[task.task_id for task in tasks],
                acceptance_criteria=ROLE_ACCEPTANCE_CRITERIA["judge"],
            )
        )
    now = utc_now()
    return TaskGraph(
        graph_id=run_id,
        run_id=run_id,
        title="LangGraph 求职协作任务图",
        query=query,
        source="interactive",
        mode=mode,
        status="queued",
        progress=0,
        created_at=now,
        updated_at=now,
        tasks=tasks,
    )


def build_daily_task_graph(
    *,
    run_id: str,
    target_date: str,
    controller_description: str,
    role_descriptions: dict[str, str],
    roles: list[RoleSpec],
    include_controller: bool = True,
) -> TaskGraph:
    tasks: list[TaskNode] = []
    if include_controller:
        tasks.append(
            TaskNode(
                task_id="controller_daily",
                title="Chief of Staff：发布每日求职总报",
                description=controller_description,
                role_id="controller",
                display_name="Chief of Staff",
                phase="delivery",
                status="ready",
                acceptance_criteria=[
                    "包含新增岗位与官方链接",
                    "给出投递优先级和三项当天行动",
                    "成功推送到 Agent 小镇群",
                ],
            )
        )
    for role in roles:
        description = role_descriptions.get(
            role.role_id,
            "日报没有识别到该角色的新任务。",
        )
        dependencies = ["controller_daily"] if include_controller else []
        if role.role_id == "judge":
            dependencies = [
                *(
                    ["controller_daily"]
                    if include_controller
                    else []
                ),
                *[
                    candidate.role_id
                    for candidate in roles
                    if candidate.role_id != "judge"
                ],
            ]
        tasks.append(
            TaskNode(
                task_id=role.role_id,
                title=f"{role.display_name}：接收并解释今日岗位任务",
                description=description[:4000],
                role_id=role.role_id,
                display_name=role.display_name,
                phase="delivery",
                status="blocked",
                depends_on=dependencies,
                acceptance_criteria=[
                    *ROLE_ACCEPTANCE_CRITERIA.get(
                        role.role_id,
                        ["说明角色负责事项", "给出下一步行动"],
                    ),
                    "成功推送到 Agent 小镇群",
                ],
            )
        )
    now = utc_now()
    return TaskGraph(
        graph_id=run_id,
        run_id=run_id,
        title=f"{target_date} 新岗位拆解与日报分发",
        query=f"拆解并推送 {target_date} 新增岗位与求职行动",
        source="daily",
        mode="daily",
        status="queued",
        progress=0,
        created_at=now,
        updated_at=now,
        tasks=tasks,
    )


class TaskGraphStore:
    """Cross-process, append-by-file task graph store with atomic updates."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.directory / ".task-graphs.lock"

    def _path(self, run_id: str) -> Path:
        safe_id = "".join(
            character
            for character in run_id
            if character.isalnum() or character in {"-", "_"}
        )
        if not safe_id:
            raise ValueError("run_id cannot be empty")
        return self.directory / f"{safe_id}.json"

    @contextmanager
    def _locked(self):
        descriptor = os.open(
            self.lock_path,
            os.O_CREAT | os.O_RDWR,
            0o600,
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    @staticmethod
    def _recompute(graph: TaskGraph) -> TaskGraph:
        by_id = {task.task_id: task for task in graph.tasks}
        for task in graph.tasks:
            if task.status not in {"blocked", "ready"}:
                continue
            dependencies = [by_id[item] for item in task.depends_on if item in by_id]
            if not dependencies or all(
                dependency.status == "completed"
                for dependency in dependencies
            ):
                task.status = "ready"
            else:
                task.status = "blocked"
        graph.progress = (
            round(sum(task.progress for task in graph.tasks) / len(graph.tasks))
            if graph.tasks
            else 100
        )
        statuses = {task.status for task in graph.tasks}
        if not graph.tasks or statuses == {"completed"}:
            graph.status = "completed"
        elif statuses & {"error", "timeout"}:
            graph.status = (
                "partial"
                if statuses & {"completed", "running", "ready"}
                else "error"
            )
        elif "running" in statuses or "completed" in statuses:
            graph.status = "running"
        else:
            graph.status = "queued"
        graph.updated_at = utc_now()
        return graph

    def _write(self, graph: TaskGraph) -> None:
        payload = graph.model_dump_json(indent=2) + "\n"
        descriptor, temp_name = tempfile.mkstemp(
            dir=self.directory,
            prefix=".task-graph.",
            text=True,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.replace(temp_name, self._path(graph.run_id))
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    def create(self, graph: TaskGraph) -> TaskGraph:
        with self._locked():
            path = self._path(graph.run_id)
            if path.exists():
                return TaskGraph.model_validate_json(
                    path.read_text(encoding="utf-8")
                )
            graph = self._recompute(graph)
            self._write(graph)
            return graph

    def get(self, run_id: str) -> TaskGraph | None:
        path = self._path(run_id)
        if not path.exists():
            return None
        return TaskGraph.model_validate_json(path.read_text(encoding="utf-8"))

    def list(self, limit: int = 50) -> list[TaskGraph]:
        paths = sorted(
            self.directory.glob("*.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        return [
            TaskGraph.model_validate_json(path.read_text(encoding="utf-8"))
            for path in paths[: max(1, min(limit, 500))]
        ]

    def update_task(
        self,
        run_id: str,
        task_id: str,
        *,
        status: TaskStatus,
        model: str | None = None,
        output: str = "",
        error: str | None = None,
    ) -> TaskGraph:
        with self._locked():
            graph = self.get(run_id)
            if graph is None:
                raise KeyError(f"unknown task graph: {run_id}")
            task = next(
                (item for item in graph.tasks if item.task_id == task_id),
                None,
            )
            if task is None:
                raise KeyError(f"unknown task: {task_id}")
            task.status = status
            if status == "running":
                task.progress = 50
                task.started_at = task.started_at or utc_now()
            elif status == "completed":
                task.progress = 100
                task.completed_at = utc_now()
            elif status in {"error", "timeout"}:
                task.progress = 100
                task.completed_at = utc_now()
            if model:
                task.model = model
            if output:
                normalized = " ".join(output.split())
                task.output_excerpt = normalized[:600]
            task.error = error
            graph = self._recompute(graph)
            self._write(graph)
            return graph
