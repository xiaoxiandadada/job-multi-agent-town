from __future__ import annotations

import asyncio
import json
import tempfile
import time
from pathlib import Path

from job_agent_harness.langgraph_orchestrator import LangGraphOrchestrator
from job_agent_harness.model_client import MockModelClient
from job_agent_harness.models import RunRequest
from job_agent_harness.orchestrator import MultiAgentOrchestrator
from job_agent_harness.registry import RoleRegistry
from job_agent_harness.runtime import ROOT


async def measured(orchestrator, request):
    started = time.perf_counter()
    report = await orchestrator.run(request)
    return {
        "wall_latency_ms": round((time.perf_counter() - started) * 1000, 2),
        "calls": report.metrics.model_calls,
        "roles": report.metrics.selected_roles,
    }


async def main():
    with tempfile.TemporaryDirectory() as temp_dir:
        runtime_path = Path(temp_dir) / "roles.json"
        registry = RoleRegistry.from_seed(
            runtime_path,
            ROOT / "configs" / "roles.json",
        )
        request = RunRequest(
            query="分析 AI Agent 岗位并生成简历、作品和面试建议",
            requested_roles=[
                "jd_analyst",
                "job_knowledge_curator",
                "resume_strategist",
                "portfolio_coach",
                "interview_coach",
            ],
            mode="collaborative",
        )
        baseline = MultiAgentOrchestrator(
            registry,
            MockModelClient(latency_seconds=0.05),
        )
        graph_base = MultiAgentOrchestrator(
            registry,
            MockModelClient(latency_seconds=0.05),
        )
        results = {
            "asyncio_harness": await measured(baseline, request),
            "langgraph_adapter": await measured(
                LangGraphOrchestrator(graph_base),
                request,
            ),
        }
        print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
