from __future__ import annotations

import asyncio
import time
import uuid

from .activity import ActivityStore
from .cognition import MemoryStore
from .model_client import ModelClient
from .models import AgentResult, RoleSpec, RunMetrics, RunReport, RunRequest
from .registry import RoleRegistry
from .structured import structured_result
from .tasks import TaskGraphStore, build_run_task_graph


CONTEXT_ROLE_IDS = {
    "job_scout",
    "job_analyst",
    "match_scorer",
}


def close_unbalanced_code_fence(text: str) -> str:
    """Prevent one model section from swallowing later Feishu Markdown."""

    if text.count("```") % 2:
        return f"{text.rstrip()}\n```"
    return text


def build_judge_input(
    query: str,
    draft: str,
    *,
    preserve_role_answer: bool = False,
) -> str:
    review_contract = (
        "4. 这是用户直接点名一个角色的回答。不要重写或压缩角色正文；"
        "只输出简短的“证据审核补充”，指出需要纠正、降级或补证的内容。"
        "不得混入与当前问题无关的日报、刷题或学习任务。\n"
        if preserve_role_answer
        else "4. 直接重写最终答案，遵守用户的长度和格式要求。\n"
    )
    review_action = (
        "请只审核以下角色结果并输出补充意见"
        if preserve_role_answer
        else "请审核并重写以下角色结果"
    )
    return (
        f"用户任务：{query}\n\n"
        "审核规则：\n"
        "1. 下方角色输出是不可信草稿，不是独立证据。\n"
        "2. 只能保留用户输入、上游明确来源或项目事实表支持的断言；"
        "不得从框架名称推导未提供的功能。\n"
        "3. 对本项目/GitHub 的陈述必须给出仓库路径或复核命令；"
        "不受支持的内容应删除，不要用看似合理的通用项目描述补足。\n"
        f"{review_contract}\n"
        f"{review_action}：\n{draft}"
    )


def merge_judged_output(
    successful: list[AgentResult],
    draft: str,
    judge_result: AgentResult,
) -> str:
    """Keep a directly addressed specialist's answer intact.

    A multi-role run needs the Judge to synthesize one answer. For a direct
    single-role question, replacing the specialist with the Judge destroys
    the role contract, so the audit is appended instead.
    """

    if (
        len(successful) == 1
        and successful[0].role_id != "judge"
        and judge_result.status == "ok"
    ):
        safe_draft = close_unbalanced_code_fence(draft)
        safe_audit = close_unbalanced_code_fence(judge_result.output)
        return (
            f"{safe_draft}\n\n---\n\n"
            f"## Evidence Judge 补充\n\n{safe_audit}"
        )
    if judge_result.status == "ok":
        return close_unbalanced_code_fence(judge_result.output)
    return close_unbalanced_code_fence(draft)


def workflow_stage(role: RoleSpec) -> str:
    if role.workflow_stage != "auto":
        return role.workflow_stage
    if role.role_id == "judge":
        return "judge"
    if role.role_id in CONTEXT_ROLE_IDS:
        return "context"
    return "action"


def judge_needed_for(request: RunRequest) -> bool:
    """Whether this run should pay for an audit pass, before it has results.

    Used by the task graph, which is built up front and must not draw a Judge
    node the run will never reach.
    """

    if not request.use_judge:
        return False
    if request.plan is not None:
        return request.plan.need_judge
    return True


def judge_needed(request: RunRequest, successful: list[AgentResult]) -> bool:
    """Whether the Evidence Judge earns its call on this run.

    Cross-checking several specialists against each other is the Judge's real
    job, and with two or more outputs it always runs. For a single output the
    Judge only appended an "evidence audit" section (see ``merge_judged_output``)
    — worth a call when the planner flagged the facts as error-prone, not worth
    one for a direct question the specialist already answered.

    With no plan at all the single-role case skips the audit too: that is the
    latency the caller asked to remove, and ``use_judge=False`` was always
    available to callers who want to force it off.
    """

    if not request.use_judge or not successful:
        return False
    if len(successful) > 1:
        return True
    if request.plan is not None:
        return request.plan.need_judge
    return False


class MultiAgentOrchestrator:
    def __init__(
        self,
        registry: RoleRegistry,
        model_client: ModelClient,
        max_concurrency: int = 4,
        activity_store: ActivityStore | None = None,
        memory_store: MemoryStore | None = None,
        task_graph_store: TaskGraphStore | None = None,
        orchestrator_name: str = "asyncio",
    ):
        self.registry = registry
        self.model_client = model_client
        self.semaphore = asyncio.Semaphore(max(1, max_concurrency))
        self.activity_store = activity_store
        self.memory_store = memory_store
        self.task_graph_store = task_graph_store
        self.orchestrator_name = orchestrator_name

    def _record(self, **values) -> None:
        if self.activity_store is not None:
            self.activity_store.record(
                orchestrator=self.orchestrator_name,
                **values,
            )
        if (
            self.memory_store is not None
            and values.get("kind") == "handoff_created"
        ):
            summary = values.get("output", "")
            for role_id in values.get("target_role_ids") or []:
                self.memory_store.append(
                    role_id=role_id,
                    run_id=values["run_id"],
                    kind="handoff",
                    text=summary or "收到上游 Agent 的证据交接",
                )

    def create_task_graph(
        self,
        *,
        run_id: str,
        request: RunRequest,
        selected_roles: list[RoleSpec],
    ) -> None:
        if self.task_graph_store is None:
            return
        graph = self.task_graph_store.create(
            build_run_task_graph(
                run_id=run_id,
                query=request.query,
                roles=selected_roles,
                mode=request.mode,
                use_judge=judge_needed_for(request),
                plan=request.plan,
            )
        )
        self._record(
            run_id=run_id,
            kind="task_graph_created",
            status="completed",
            phase="route",
            mode=request.mode,
            output=f"已拆解 {len(graph.tasks)} 个任务节点",
            selected_role_ids=[
                task.role_id
                for task in graph.tasks
                if task.role_id not in {"controller", "judge"}
            ],
            metrics={
                "task_count": len(graph.tasks),
                "dependency_count": sum(
                    len(task.depends_on) for task in graph.tasks
                ),
                "plan_source": (
                    request.plan.source if request.plan is not None else "keywords"
                ),
            },
        )

    def _task_for_role(self, run_id: str, role_id: str):
        if self.task_graph_store is None:
            return None
        graph = self.task_graph_store.get(run_id)
        if graph is None:
            return None
        return next(
            (task for task in graph.tasks if task.role_id == role_id),
            None,
        )

    def apply_run_overrides(self, role, request: RunRequest):
        """A role as this run needs it.

        Only role ids travel through the LangGraph state, so every node
        re-reads the role from the registry. Per-run knobs have to be re-applied
        there or they quietly vanish between the route and the action node.
        """

        if request.timeout_seconds is None:
            return role
        return role.model_copy(update={"timeout_seconds": request.timeout_seconds})

    def role_for_run(self, role_id: str, request: RunRequest):
        return self.apply_run_overrides(self.registry.get(role_id), request)

    def select_roles(self, request: RunRequest):
        """Who works this run.

        Precedence is explicit request → plan → keywords. The keyword pass is
        last on purpose: overlapping ``trigger_keywords`` are what made a
        one-role question wake six specialists, so it is now only the fallback
        for callers with no plan (the patrol, the daily push) and for a planner
        that failed to produce one.
        """

        roles = [
            role
            for role in self.registry.list_roles()
            if workflow_stage(role) != "judge"
        ]
        if request.requested_roles:
            selected = [
                self.registry.get(role_id) for role_id in request.requested_roles
            ]
        elif request.plan is not None and request.plan.subtasks:
            by_id = {role.role_id: role for role in roles}
            selected = [
                by_id[role_id]
                for role_id in request.plan.role_ids()
                if role_id in by_id
            ]
        else:
            lowered = request.query.casefold()
            selected = [
                role
                for role in roles
                if any(keyword.casefold() in lowered for keyword in role.trigger_keywords)
            ]
        if not selected:
            selected = roles[:1]
        if request.mode == "single":
            selected = selected[:1]
        else:
            selected = list({role.role_id: role for role in selected}.values())
        return [self.apply_run_overrides(role, request) for role in selected]

    async def _complete_role(self, role, query: str, images=()):
        async with self.semaphore:
            return await self.model_client.complete(role, query, images=images)

    async def _run_role(
        self,
        role,
        query: str,
        *,
        run_id: str | None = None,
        phase: str = "action",
        images=(),
    ) -> AgentResult:
        started = time.perf_counter()
        role_query = query
        task = self._task_for_role(run_id, role.role_id) if run_id else None
        if task is not None and self.task_graph_store is not None:
            self.task_graph_store.update_task(
                run_id,
                task.task_id,
                status="running",
                model=self.model_client.model_for(role)
                if hasattr(self.model_client, "model_for")
                else None,
            )
            self._record(
                run_id=run_id,
                kind="task_started",
                status="running",
                phase=task.phase,
                role_id=role.role_id,
                display_name=role.display_name,
                task_id=task.task_id,
                task_title=task.title,
                depends_on=task.depends_on,
                progress=50,
            )
        if self.memory_store is not None and run_id:
            memories = self.memory_store.retrieve(
                role_id=role.role_id,
                query=query,
                limit=4,
            )
            if memories:
                memory_context = "\n".join(
                    (
                        f"- [{item.memory.kind}] {item.memory.text} "
                        f"(score={item.score:.2f})"
                    )
                    for item in memories
                )
                role_query = (
                    f"{query}\n\n"
                    "以下是该角色从过去任务中检索到的长期记忆。"
                    "仅在相关且仍然有效时使用，遇到过时信息要重新核验：\n"
                    f"{memory_context}"
                )
                self._record(
                    run_id=run_id,
                    kind="memory_retrieved",
                    status="completed",
                    phase=phase,
                    role_id=role.role_id,
                    display_name=role.display_name,
                    output=f"检索到 {len(memories)} 条长期记忆",
                )
            plan = self.memory_store.ensure_plan(
                role_id=role.role_id,
                run_id=run_id,
                schedule=role.schedule,
                query=query,
            )
            self._record(
                run_id=run_id,
                kind="plan_updated",
                status="completed",
                phase=phase,
                role_id=role.role_id,
                display_name=role.display_name,
                output=plan.text,
            )
        if run_id:
            self._record(
                run_id=run_id,
                kind="agent_started",
                status="running",
                phase=phase,
                role_id=role.role_id,
                display_name=role.display_name,
                query=role_query,
            )
        try:
            reply = await asyncio.wait_for(
                self._complete_role(role, role_query, images),
                timeout=role.timeout_seconds,
            )
            # A data role returns JSON; everyone downstream — the Judge, the chat
            # window, Feishu — reads Markdown. Render once here so no consumer has
            # to know which roles are structured. The parsed object rides along
            # because the apply-nudge has to compare a score, not read a table.
            rendered, match_report = structured_result(role.role_id, reply.content)
            result = AgentResult(
                role_id=role.role_id,
                display_name=role.display_name,
                output=rendered,
                match_report=match_report,
                latency_ms=(time.perf_counter() - started) * 1000,
                input_tokens=reply.input_tokens,
                output_tokens=reply.output_tokens,
                model=reply.model,
            )
        except asyncio.TimeoutError:
            result = AgentResult(
                role_id=role.role_id,
                display_name=role.display_name,
                status="timeout",
                latency_ms=(time.perf_counter() - started) * 1000,
                error="agent timed out",
            )
        except Exception as exc:
            result = AgentResult(
                role_id=role.role_id,
                display_name=role.display_name,
                status="error",
                latency_ms=(time.perf_counter() - started) * 1000,
                error=str(exc),
            )
        if run_id:
            self._record(
                run_id=run_id,
                kind="agent_completed",
                status=result.status,
                phase=phase,
                role_id=result.role_id,
                display_name=result.display_name,
                model=result.model,
                latency_ms=result.latency_ms,
                output=result.output,
                error=result.error,
            )
            if task is not None and self.task_graph_store is not None:
                task_status = (
                    "completed" if result.status == "ok" else result.status
                )
                self.task_graph_store.update_task(
                    run_id,
                    task.task_id,
                    status=task_status,
                    model=result.model,
                    output=result.output,
                    error=result.error,
                )
                self._record(
                    run_id=run_id,
                    kind="task_completed",
                    status=(
                        "completed"
                        if result.status == "ok"
                        else result.status
                    ),
                    phase=task.phase,
                    role_id=role.role_id,
                    display_name=role.display_name,
                    model=result.model,
                    task_id=task.task_id,
                    task_title=task.title,
                    depends_on=task.depends_on,
                    progress=100,
                    output=result.output,
                    error=result.error,
                )
            if self.memory_store is not None:
                observation_text = (
                    result.output
                    if result.status == "ok"
                    else f"{result.status}: {result.error or '没有输出'}"
                )
                self.memory_store.append(
                    role_id=role.role_id,
                    run_id=run_id,
                    kind="observation",
                    text=observation_text,
                )
                reflection = self.memory_store.maybe_reflect(
                    role_id=role.role_id,
                    run_id=run_id,
                )
                if reflection is not None:
                    self._record(
                        run_id=run_id,
                        kind="reflection_created",
                        status="completed",
                        phase=phase,
                        role_id=role.role_id,
                        display_name=role.display_name,
                        output=reflection.text,
                    )
        return result

    async def _run_parallel(
        self,
        roles,
        query: str,
        *,
        run_id: str | None = None,
        phase: str = "action",
        images=(),
    ) -> list[AgentResult]:
        return list(
            await asyncio.gather(
                *(
                    self._run_role(
                        role,
                        query,
                        run_id=run_id,
                        phase=phase,
                        images=images,
                    )
                    for role in roles
                )
            )
        )

    @staticmethod
    def _format_results(results: list[AgentResult]) -> str:
        return "\n\n".join(
            f"## {result.display_name}\n"
            f"{close_unbalanced_code_fence(result.output)}"
            for result in results
            if result.status == "ok"
        )

    async def run(
        self,
        request: RunRequest,
        *,
        run_id: str | None = None,
    ) -> RunReport:
        # The caller may own the id: the chief of staff records its intake and
        # closing under the same run so the timeline and the replay page show
        # one run, not three unrelated fragments.
        run_id = run_id or str(uuid.uuid4())
        started = time.perf_counter()
        self._record(
            run_id=run_id,
            kind="run_started",
            status="running",
            phase="route",
            mode=request.mode,
            query=request.query,
            metrics={"origin": request.origin},
        )
        selected = self.select_roles(request)
        self.create_task_graph(
            run_id=run_id,
            request=request,
            selected_roles=selected,
        )
        self._record(
            run_id=run_id,
            kind="route_completed",
            status="completed",
            phase="route",
            mode=request.mode,
            selected_role_ids=[role.role_id for role in selected],
        )
        if request.mode == "parallel":
            self._record(
                run_id=run_id,
                kind="phase_started",
                status="running",
                phase="action",
                mode=request.mode,
                selected_role_ids=[role.role_id for role in selected],
            )
            results = await self._run_parallel(
                selected,
                request.query,
                run_id=run_id,
                phase="action",
                images=request.images,
            )
        elif request.mode == "collaborative":
            discovery_roles = [
                role for role in selected if role.role_id == "job_scout"
            ]
            analysis_roles = [
                role
                for role in selected
                if role.role_id != "job_scout"
                and workflow_stage(role) == "context"
            ]
            action_roles = [
                role for role in selected if workflow_stage(role) == "action"
            ]
            discovery_results: list[AgentResult] = []
            if discovery_roles:
                self._record(
                    run_id=run_id,
                    kind="phase_started",
                    status="running",
                    phase="discovery",
                    mode=request.mode,
                    selected_role_ids=[
                        role.role_id for role in discovery_roles
                    ],
                )
                discovery_results = await self._run_parallel(
                    discovery_roles,
                    request.query,
                    run_id=run_id,
                    phase="discovery",
                    images=request.images,
                )
            discovery_output = self._format_results(discovery_results)
            analysis_query = request.query
            if discovery_output:
                analysis_query = (
                    f"用户原始任务：{request.query}\n\n"
                    "以下是 Job Scout 先完成的岗位与来源证据。"
                    "请基于该证据做专业分析，不要重新猜测岗位事实：\n\n"
                    f"{discovery_output}"
                )
            analysis_results: list[AgentResult] = []
            if analysis_roles:
                if discovery_output:
                    self._record(
                        run_id=run_id,
                        kind="handoff_created",
                        status="completed",
                        phase="analysis",
                        mode=request.mode,
                        output=discovery_output,
                        source_role_ids=[
                            result.role_id
                            for result in discovery_results
                        ],
                        target_role_ids=[
                            role.role_id for role in analysis_roles
                        ],
                    )
                self._record(
                    run_id=run_id,
                    kind="phase_started",
                    status="running",
                    phase="analysis",
                    mode=request.mode,
                    selected_role_ids=[
                        role.role_id for role in analysis_roles
                    ],
                )
                analysis_results = await self._run_parallel(
                    analysis_roles,
                    analysis_query,
                    run_id=run_id,
                    phase="analysis",
                    images=request.images,
                )
            context_results = [
                *discovery_results,
                *analysis_results,
            ]
            context_output = self._format_results(context_results)
            action_query = request.query
            if context_output:
                action_query = (
                    f"用户原始任务：{request.query}\n\n"
                    "以下是上游 Agent 已完成的岗位发现、JD 分析与知识补充。"
                    "请基于这些证据继续工作，并指出仍待核验的内容：\n\n"
                    f"{context_output}"
                )
                self._record(
                    run_id=run_id,
                    kind="handoff_created",
                    status="completed",
                    phase="action",
                    mode=request.mode,
                    output=context_output,
                    source_role_ids=[
                        result.role_id for result in context_results
                    ],
                    target_role_ids=[
                        role.role_id for role in action_roles
                    ],
                )
            self._record(
                run_id=run_id,
                kind="phase_started",
                status="running",
                phase="action",
                mode=request.mode,
                selected_role_ids=[role.role_id for role in action_roles],
            )
            action_results = await self._run_parallel(
                action_roles,
                action_query,
                run_id=run_id,
                phase="action",
                images=request.images,
            )
            results = [*context_results, *action_results]
        else:
            self._record(
                run_id=run_id,
                kind="phase_started",
                status="running",
                phase="action",
                mode=request.mode,
                selected_role_ids=[role.role_id for role in selected],
            )
            results = []
            for role in selected:
                results.append(
                    await self._run_role(
                        role,
                        request.query,
                        run_id=run_id,
                        phase="action",
                        images=request.images,
                    )
                )

        successful = [result for result in results if result.status == "ok"]
        final_output = self._format_results(successful)
        judge_calls = 0
        if judge_needed(request, successful):
            try:
                judge = self.role_for_run("judge", request)
                judge_input = build_judge_input(
                    request.query,
                    final_output,
                    preserve_role_answer=(
                        len(successful) == 1
                        and successful[0].role_id != "judge"
                    ),
                )
                self._record(
                    run_id=run_id,
                    kind="phase_started",
                    status="running",
                    phase="judge",
                    mode=request.mode,
                    selected_role_ids=["judge"],
                )
                judge_result = await self._run_role(
                    judge,
                    judge_input,
                    run_id=run_id,
                    phase="judge",
                )
                results.append(judge_result)
                judge_calls = 1
                final_output = merge_judged_output(
                    successful,
                    final_output,
                    judge_result,
                )
            except KeyError:
                pass

        wall_ms = (time.perf_counter() - started) * 1000
        sum_ms = sum(result.latency_ms for result in results)
        metrics = RunMetrics(
            selected_roles=len(selected),
            completed_roles=sum(result.status == "ok" for result in results),
            failed_roles=sum(result.status != "ok" for result in results),
            model_calls=len(selected) + judge_calls,
            wall_latency_ms=wall_ms,
            sum_agent_latency_ms=sum_ms,
            input_tokens=sum(result.input_tokens for result in results),
            output_tokens=sum(result.output_tokens for result in results),
            parallel_speedup_estimate=(sum_ms / wall_ms if wall_ms else 1.0),
        )
        report = RunReport(
            run_id=run_id,
            query=request.query,
            mode=request.mode,
            role_registry_version=self.registry.version,
            results=results,
            final_output=final_output,
            metrics=metrics,
        )
        self._record(
            run_id=run_id,
            kind="run_completed",
            status="completed",
            phase="system",
            mode=request.mode,
            output=final_output,
            metrics=metrics.model_dump(),
        )
        return report
