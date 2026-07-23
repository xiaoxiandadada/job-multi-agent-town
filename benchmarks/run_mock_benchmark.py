from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from job_agent_harness.model_client import MockModelClient
from job_agent_harness.models import RoleSpec, RunRequest
from job_agent_harness.orchestrator import MultiAgentOrchestrator
from job_agent_harness.registry import RoleRegistry


def build_registry(path: Path) -> RoleRegistry:
    registry = RoleRegistry(path)
    registry.replace_all(
        [
            RoleSpec(
                role_id="jd_analyst",
                display_name="JD 分析师",
                goal="提取岗位要求和技能缺口",
                system_prompt="提取 JD 证据。",
                trigger_keywords=["JD"],
            ),
            RoleSpec(
                role_id="resume_strategist",
                display_name="简历策略师",
                goal="生成有证据的简历建议",
                system_prompt="根据已有证据生成简历建议。",
                trigger_keywords=["简历"],
            ),
            RoleSpec(
                role_id="portfolio_coach",
                display_name="作品教练",
                goal="生成可验证的作品动作",
                system_prompt="根据岗位缺口生成作品动作。",
                trigger_keywords=["作品"],
            ),
        ]
    )
    return registry


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        registry = build_registry(Path(tmp) / "roles.json")
        query = "分析 JD、优化简历，并规划作品"
        rows = []
        for mode in ("single", "sequential", "parallel"):
            report = await MultiAgentOrchestrator(
                registry, MockModelClient(latency_seconds=0.1)
            ).run(RunRequest(query=query, mode=mode, use_judge=False))
            rows.append(
                {
                    "mode": mode,
                    "roles": report.metrics.selected_roles,
                    "wall_latency_ms": round(report.metrics.wall_latency_ms, 2),
                    "sum_agent_latency_ms": round(
                        report.metrics.sum_agent_latency_ms, 2
                    ),
                    "speedup": round(
                        report.metrics.parallel_speedup_estimate, 2
                    ),
                }
            )
        print(json.dumps(rows, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
