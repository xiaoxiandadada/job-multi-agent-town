import pytest

from job_agent_harness.model_client import MockModelClient
from job_agent_harness.models import RoleSpec, RunRequest
from job_agent_harness.orchestrator import MultiAgentOrchestrator
from job_agent_harness.registry import RoleRegistry


def role(role_id, keyword, profile="default"):
    return RoleSpec(
        role_id=role_id,
        display_name=role_id,
        goal=f"处理 {keyword} 方向的求职任务",
        system_prompt=f"你负责 {keyword}，输出证据。",
        trigger_keywords=[keyword],
        model_profile=profile,
    )


@pytest.fixture
def registry(tmp_path):
    value = RoleRegistry(tmp_path / "roles.json")
    value.replace_all(
        [
            role("jd_analyst", "JD"),
            role("resume_strategist", "简历"),
            role("judge", "不会自动触发", "judge"),
        ]
    )
    return value


async def test_router_selects_only_relevant_roles(registry):
    client = MockModelClient()
    orchestrator = MultiAgentOrchestrator(registry, client)

    report = await orchestrator.run(
        RunRequest(query="分析 JD 并优化简历", use_judge=False)
    )

    assert [result.role_id for result in report.results] == [
        "jd_analyst",
        "resume_strategist",
    ]
    assert report.metrics.model_calls == 2


async def test_parallel_mode_reduces_wall_latency(registry):
    sequential_client = MockModelClient(latency_seconds=0.05)
    parallel_client = MockModelClient(latency_seconds=0.05)

    sequential = await MultiAgentOrchestrator(
        registry, sequential_client
    ).run(RunRequest(query="分析 JD 并优化简历", mode="sequential", use_judge=False))
    parallel = await MultiAgentOrchestrator(
        registry, parallel_client
    ).run(RunRequest(query="分析 JD 并优化简历", mode="parallel", use_judge=False))

    assert parallel.metrics.wall_latency_ms < sequential.metrics.wall_latency_ms * 0.75
    assert parallel.metrics.parallel_speedup_estimate > 1.5


async def test_judge_is_a_separate_model_call(registry):
    client = MockModelClient()
    report = await MultiAgentOrchestrator(registry, client).run(
        RunRequest(query="分析 JD", use_judge=True)
    )

    assert client.calls == ["jd_analyst", "judge"]
    assert report.metrics.model_calls == 2
    assert report.results[-1].role_id == "judge"

