from __future__ import annotations

import time
import uuid
from typing import Any, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from .models import AgentResult, RunMetrics, RunReport, RunRequest
from .orchestrator import (
    MultiAgentOrchestrator,
    build_judge_input,
    merge_judged_output,
    workflow_stage,
)


class JobAgentGraphState(TypedDict, total=False):
    run_id: str
    request: dict[str, Any]
    selected_role_ids: list[str]
    discovery_role_ids: list[str]
    analysis_role_ids: list[str]
    action_role_ids: list[str]
    discovery_results: list[dict[str, Any]]
    analysis_results: list[dict[str, Any]]
    action_results: list[dict[str, Any]]
    results: list[dict[str, Any]]
    final_output: str
    judge_called: bool


class LangGraphOrchestrator:
    """LangGraph adapter that preserves the existing role/model contracts."""

    def __init__(
        self,
        base: MultiAgentOrchestrator,
        checkpointer=None,
    ):
        self.base = base
        self.base.orchestrator_name = "langgraph"
        self.checkpointer = checkpointer or InMemorySaver()
        self.graph = self._build_graph().compile(checkpointer=self.checkpointer)

    def _build_graph(self) -> StateGraph:
        builder = StateGraph(JobAgentGraphState)
        builder.add_node("route", self._route)
        builder.add_node("discovery_phase", self._discovery_phase)
        builder.add_node("analysis_phase", self._analysis_phase)
        builder.add_node("action_phase", self._action_phase)
        builder.add_node("judge", self._judge)
        builder.add_edge(START, "route")
        builder.add_conditional_edges(
            "route",
            self._after_route,
            {
                "discovery_phase": "discovery_phase",
                "analysis_phase": "analysis_phase",
                "action_phase": "action_phase",
                "judge": "judge",
            },
        )
        builder.add_conditional_edges(
            "discovery_phase",
            self._after_discovery,
            {
                "analysis_phase": "analysis_phase",
                "action_phase": "action_phase",
                "judge": "judge",
            },
        )
        builder.add_conditional_edges(
            "analysis_phase",
            self._after_analysis,
            {
                "action_phase": "action_phase",
                "judge": "judge",
            },
        )
        builder.add_edge("action_phase", "judge")
        builder.add_edge("judge", END)
        return builder

    async def _route(self, state: JobAgentGraphState) -> dict[str, Any]:
        request = RunRequest.model_validate(state["request"])
        selected = self.base.select_roles(request)
        selected_ids = [role.role_id for role in selected]
        self.base.create_task_graph(
            run_id=state["run_id"],
            request=request,
            selected_roles=selected,
        )
        self.base._record(
            run_id=state["run_id"],
            kind="route_completed",
            status="completed",
            phase="route",
            mode=request.mode,
            selected_role_ids=selected_ids,
        )
        if request.mode != "collaborative":
            return {
                "selected_role_ids": selected_ids,
                "discovery_role_ids": [],
                "analysis_role_ids": [],
                "action_role_ids": selected_ids,
            }
        return {
            "selected_role_ids": selected_ids,
            "discovery_role_ids": [
                role_id
                for role_id in selected_ids
                if role_id == "job_scout"
            ],
            "analysis_role_ids": [
                role_id
                for role_id in selected_ids
                if role_id != "job_scout"
                and workflow_stage(self.base.registry.get(role_id)) == "context"
            ],
            "action_role_ids": [
                role_id
                for role_id in selected_ids
                if workflow_stage(self.base.registry.get(role_id)) == "action"
            ],
        }

    @staticmethod
    def _after_route(state: JobAgentGraphState) -> str:
        if state.get("discovery_role_ids"):
            return "discovery_phase"
        if state.get("analysis_role_ids"):
            return "analysis_phase"
        if state.get("action_role_ids"):
            return "action_phase"
        return "judge"

    async def _discovery_phase(
        self,
        state: JobAgentGraphState,
    ) -> dict[str, Any]:
        request = RunRequest.model_validate(state["request"])
        roles = [
            self.base.registry.get(role_id)
            for role_id in state.get("discovery_role_ids", [])
        ]
        self.base._record(
            run_id=state["run_id"],
            kind="phase_started",
            status="running",
            phase="discovery",
            mode=request.mode,
            selected_role_ids=[role.role_id for role in roles],
        )
        results = await self.base._run_parallel(
            roles,
            request.query,
            run_id=state["run_id"],
            phase="discovery",
        )
        return {
            "discovery_results": [
                result.model_dump() for result in results
            ]
        }

    @staticmethod
    def _after_discovery(state: JobAgentGraphState) -> str:
        if state.get("analysis_role_ids"):
            return "analysis_phase"
        return "action_phase" if state.get("action_role_ids") else "judge"

    async def _analysis_phase(
        self,
        state: JobAgentGraphState,
    ) -> dict[str, Any]:
        request = RunRequest.model_validate(state["request"])
        discovery_results = [
            AgentResult.model_validate(value)
            for value in state.get("discovery_results", [])
        ]
        discovery_output = self.base._format_results(discovery_results)
        query = request.query
        if discovery_output:
            query = (
                f"用户原始任务：{request.query}\n\n"
                "以下是岗位侦察员先完成的岗位与来源证据。"
                "请基于该证据做专业分析，不要重新猜测岗位事实：\n\n"
                f"{discovery_output}"
            )
        roles = [
            self.base.registry.get(role_id)
            for role_id in state.get("analysis_role_ids", [])
        ]
        if discovery_output and roles:
            self.base._record(
                run_id=state["run_id"],
                kind="handoff_created",
                status="completed",
                phase="analysis",
                mode=request.mode,
                output=discovery_output,
                source_role_ids=[
                    result.role_id for result in discovery_results
                ],
                target_role_ids=[role.role_id for role in roles],
            )
        self.base._record(
            run_id=state["run_id"],
            kind="phase_started",
            status="running",
            phase="analysis",
            mode=request.mode,
            selected_role_ids=[role.role_id for role in roles],
        )
        results = await self.base._run_parallel(
            roles,
            query,
            run_id=state["run_id"],
            phase="analysis",
        )
        return {
            "analysis_results": [
                result.model_dump() for result in results
            ]
        }

    @staticmethod
    def _after_analysis(state: JobAgentGraphState) -> str:
        return "action_phase" if state.get("action_role_ids") else "judge"

    async def _action_phase(
        self,
        state: JobAgentGraphState,
    ) -> dict[str, Any]:
        request = RunRequest.model_validate(state["request"])
        discovery_results = [
            AgentResult.model_validate(value)
            for value in state.get("discovery_results", [])
        ]
        analysis_results = [
            AgentResult.model_validate(value)
            for value in state.get("analysis_results", [])
        ]
        context_results = [*discovery_results, *analysis_results]
        context_output = self.base._format_results(context_results)
        query = request.query
        if context_output:
            query = (
                f"用户原始任务：{request.query}\n\n"
                "以下是上游 Agent 已完成的岗位发现、JD 分析与知识补充。"
                "请基于这些证据继续工作，并指出仍待核验的内容：\n\n"
                f"{context_output}"
            )
        roles = [
            self.base.registry.get(role_id)
            for role_id in state.get("action_role_ids", [])
        ]
        if context_output and roles:
            self.base._record(
                run_id=state["run_id"],
                kind="handoff_created",
                status="completed",
                phase="action",
                mode=request.mode,
                output=context_output,
                source_role_ids=[
                    result.role_id for result in context_results
                ],
                target_role_ids=[role.role_id for role in roles],
            )
        self.base._record(
            run_id=state["run_id"],
            kind="phase_started",
            status="running",
            phase="action",
            mode=request.mode,
            selected_role_ids=[role.role_id for role in roles],
        )
        if request.mode == "sequential":
            results = []
            for role in roles:
                results.append(
                    await self.base._run_role(
                        role,
                        query,
                        run_id=state["run_id"],
                        phase="action",
                    )
                )
        else:
            results = await self.base._run_parallel(
                roles,
                query,
                run_id=state["run_id"],
                phase="action",
            )
        return {"action_results": [result.model_dump() for result in results]}

    async def _judge(self, state: JobAgentGraphState) -> dict[str, Any]:
        request = RunRequest.model_validate(state["request"])
        results = [
            AgentResult.model_validate(value)
            for value in [
                *state.get("discovery_results", []),
                *state.get("analysis_results", []),
                *state.get("action_results", []),
            ]
        ]
        successful = [result for result in results if result.status == "ok"]
        final_output = self.base._format_results(successful)
        judge_called = False
        if request.use_judge and successful:
            try:
                judge = self.base.registry.get("judge")
            except KeyError:
                pass
            else:
                self.base._record(
                    run_id=state["run_id"],
                    kind="phase_started",
                    status="running",
                    phase="judge",
                    mode=request.mode,
                    selected_role_ids=["judge"],
                )
                judge_input = build_judge_input(
                    request.query,
                    final_output,
                    preserve_role_answer=(
                        len(successful) == 1
                        and successful[0].role_id != "judge"
                    ),
                )
                judge_result = await self.base._run_role(
                    judge,
                    judge_input,
                    run_id=state["run_id"],
                    phase="judge",
                )
                results.append(judge_result)
                judge_called = True
                final_output = merge_judged_output(
                    successful,
                    final_output,
                    judge_result,
                )
        return {
            "results": [result.model_dump() for result in results],
            "final_output": final_output,
            "judge_called": judge_called,
        }

    async def run(
        self,
        request: RunRequest,
        *,
        thread_id: str | None = None,
    ) -> RunReport:
        run_id = thread_id or str(uuid.uuid4())
        started = time.perf_counter()
        self.base._record(
            run_id=run_id,
            kind="run_started",
            status="running",
            phase="route",
            mode=request.mode,
            query=request.query,
        )
        try:
            state = await self.graph.ainvoke(
                {
                    "run_id": run_id,
                    "request": request.model_dump(),
                },
                config={"configurable": {"thread_id": run_id}},
            )
        except Exception as exc:
            self.base._record(
                run_id=run_id,
                kind="run_failed",
                status="error",
                phase="system",
                mode=request.mode,
                error=str(exc),
            )
            raise
        results = [
            AgentResult.model_validate(value)
            for value in state.get("results", [])
        ]
        wall_ms = (time.perf_counter() - started) * 1000
        sum_ms = sum(result.latency_ms for result in results)
        selected_count = len(state.get("selected_role_ids", []))
        metrics = RunMetrics(
            selected_roles=selected_count,
            completed_roles=sum(result.status == "ok" for result in results),
            failed_roles=sum(result.status != "ok" for result in results),
            model_calls=selected_count + int(state.get("judge_called", False)),
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
            role_registry_version=self.base.registry.version,
            results=results,
            final_output=state.get("final_output", ""),
            metrics=metrics,
        )
        self.base._record(
            run_id=run_id,
            kind="run_completed",
            status="completed",
            phase="system",
            mode=request.mode,
            output=report.final_output,
            metrics=metrics.model_dump(),
        )
        return report

    def mermaid(self) -> str:
        return self.graph.get_graph().draw_mermaid()
