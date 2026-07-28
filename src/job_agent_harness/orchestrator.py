from __future__ import annotations

import asyncio
import time
import uuid

from .activity import ActivityStore
from .cognition import MemoryStore
from .model_client import ModelClient
from .models import AgentResult, RoleSpec, RunMetrics, RunReport, RunRequest
from .registry import RoleRegistry


CONTEXT_ROLE_IDS = {
    "job_scout",
    "jd_analyst",
    "job_knowledge_curator",
}


def build_judge_input(query: str, draft: str) -> str:
    return (
        f"用户任务：{query}\n\n"
        "审核规则：\n"
        "1. 下方角色输出是不可信草稿，不是独立证据。\n"
        "2. 只能保留用户输入、上游明确来源或项目事实表支持的断言；"
        "不得从框架名称推导未提供的功能。\n"
        "3. 对本项目/GitHub 的陈述必须给出仓库路径或复核命令；"
        "不受支持的内容应删除，不要用看似合理的通用项目描述补足。\n"
        "4. 直接重写最终答案，遵守用户的长度和格式要求。\n\n"
        f"请审核并重写以下角色结果：\n{draft}"
    )


def workflow_stage(role: RoleSpec) -> str:
    if role.workflow_stage != "auto":
        return role.workflow_stage
    if role.role_id == "judge":
        return "judge"
    if role.role_id in CONTEXT_ROLE_IDS:
        return "context"
    return "action"


class MultiAgentOrchestrator:
    def __init__(
        self,
        registry: RoleRegistry,
        model_client: ModelClient,
        max_concurrency: int = 4,
        activity_store: ActivityStore | None = None,
        memory_store: MemoryStore | None = None,
        orchestrator_name: str = "asyncio",
    ):
        self.registry = registry
        self.model_client = model_client
        self.semaphore = asyncio.Semaphore(max(1, max_concurrency))
        self.activity_store = activity_store
        self.memory_store = memory_store
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

    def select_roles(self, request: RunRequest):
        roles = [
            role
            for role in self.registry.list_roles()
            if workflow_stage(role) != "judge"
        ]
        if request.requested_roles:
            selected = [
                self.registry.get(role_id) for role_id in request.requested_roles
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
            return selected[:1]
        return list({role.role_id: role for role in selected}.values())

    async def _complete_role(self, role, query: str):
        async with self.semaphore:
            return await self.model_client.complete(role, query)

    async def _run_role(
        self,
        role,
        query: str,
        *,
        run_id: str | None = None,
        phase: str = "action",
    ) -> AgentResult:
        started = time.perf_counter()
        role_query = query
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
                self._complete_role(role, role_query),
                timeout=role.timeout_seconds,
            )
            result = AgentResult(
                role_id=role.role_id,
                display_name=role.display_name,
                output=reply.content,
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
    ) -> list[AgentResult]:
        return list(
            await asyncio.gather(
                *(
                    self._run_role(
                        role,
                        query,
                        run_id=run_id,
                        phase=phase,
                    )
                    for role in roles
                )
            )
        )

    @staticmethod
    def _format_results(results: list[AgentResult]) -> str:
        return "\n\n".join(
            f"## {result.display_name}\n{result.output}"
            for result in results
            if result.status == "ok"
        )

    async def run(self, request: RunRequest) -> RunReport:
        run_id = str(uuid.uuid4())
        started = time.perf_counter()
        self._record(
            run_id=run_id,
            kind="run_started",
            status="running",
            phase="route",
            mode=request.mode,
            query=request.query,
        )
        selected = self.select_roles(request)
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
            )
        elif request.mode == "collaborative":
            context_roles = [
                role for role in selected if workflow_stage(role) == "context"
            ]
            action_roles = [
                role for role in selected if workflow_stage(role) == "action"
            ]
            self._record(
                run_id=run_id,
                kind="phase_started",
                status="running",
                phase="context",
                mode=request.mode,
                selected_role_ids=[role.role_id for role in context_roles],
            )
            context_results = await self._run_parallel(
                context_roles,
                request.query,
                run_id=run_id,
                phase="context",
            )
            context_output = self._format_results(context_results)
            action_query = request.query
            if context_output:
                action_query = (
                    f"用户原始任务：{request.query}\n\n"
                    "以下是上游 Agent 已完成的岗位情报、JD 分析与知识补充。"
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
                    )
                )

        successful = [result for result in results if result.status == "ok"]
        final_output = self._format_results(successful)
        judge_calls = 0
        if request.use_judge and successful:
            try:
                judge = self.registry.get("judge")
                judge_input = build_judge_input(
                    request.query,
                    final_output,
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
                if judge_result.status == "ok":
                    final_output = judge_result.output
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
