from __future__ import annotations

import asyncio
import time
import uuid

from .model_client import ModelClient
from .models import AgentResult, RunMetrics, RunReport, RunRequest
from .registry import RoleRegistry


class MultiAgentOrchestrator:
    def __init__(
        self,
        registry: RoleRegistry,
        model_client: ModelClient,
        max_concurrency: int = 4,
    ):
        self.registry = registry
        self.model_client = model_client
        self.semaphore = asyncio.Semaphore(max(1, max_concurrency))

    def select_roles(self, request: RunRequest):
        roles = [
            role for role in self.registry.list_roles() if role.role_id != "judge"
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

    async def _run_role(self, role, query: str) -> AgentResult:
        started = time.perf_counter()
        try:
            async with self.semaphore:
                reply = await asyncio.wait_for(
                    self.model_client.complete(role, query),
                    timeout=role.timeout_seconds,
                )
            return AgentResult(
                role_id=role.role_id,
                display_name=role.display_name,
                output=reply.content,
                latency_ms=(time.perf_counter() - started) * 1000,
                input_tokens=reply.input_tokens,
                output_tokens=reply.output_tokens,
                model=reply.model,
            )
        except asyncio.TimeoutError:
            return AgentResult(
                role_id=role.role_id,
                display_name=role.display_name,
                status="timeout",
                latency_ms=(time.perf_counter() - started) * 1000,
                error="agent timed out",
            )
        except Exception as exc:
            return AgentResult(
                role_id=role.role_id,
                display_name=role.display_name,
                status="error",
                latency_ms=(time.perf_counter() - started) * 1000,
                error=str(exc),
            )

    async def run(self, request: RunRequest) -> RunReport:
        started = time.perf_counter()
        selected = self.select_roles(request)
        if request.mode == "parallel":
            results = list(
                await asyncio.gather(
                    *(self._run_role(role, request.query) for role in selected)
                )
            )
        else:
            results = []
            for role in selected:
                results.append(await self._run_role(role, request.query))

        successful = [result for result in results if result.status == "ok"]
        final_output = "\n\n".join(
            f"## {result.display_name}\n{result.output}" for result in successful
        )
        judge_calls = 0
        if request.use_judge and successful:
            try:
                judge = self.registry.get("judge")
                judge_input = (
                    f"用户任务：{request.query}\n\n请审核以下角色结果：\n{final_output}"
                )
                judge_result = await self._run_role(judge, judge_input)
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
        return RunReport(
            run_id=str(uuid.uuid4()),
            query=request.query,
            mode=request.mode,
            role_registry_version=self.registry.version,
            results=results,
            final_output=final_output,
            metrics=metrics,
        )

