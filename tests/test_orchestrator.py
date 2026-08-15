import pytest

from job_agent_harness.model_client import MockModelClient
from job_agent_harness.models import RoleSpec, RunPlan, RunRequest
from job_agent_harness.orchestrator import (
    MultiAgentOrchestrator,
    close_unbalanced_code_fence,
)
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


def test_close_unbalanced_code_fence():
    assert close_unbalanced_code_fence("```json\n{}") == "```json\n{}\n```"
    assert close_unbalanced_code_fence("```json\n{}\n```") == "```json\n{}\n```"


@pytest.fixture
def registry(tmp_path):
    value = RoleRegistry(tmp_path / "roles.json")
    value.replace_all(
        [
            role("job_analyst", "JD"),
            role("material_builder", "简历"),
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
        "job_analyst",
        "material_builder",
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


async def test_role_timeout_includes_waiting_for_concurrency_slot(tmp_path):
    timeout_registry = RoleRegistry(tmp_path / "timeout-roles.json")
    slow_roles = [
        role("first", "任务"),
        role("second", "任务"),
        role("judge", "不会自动触发", "judge"),
    ]
    slow_roles[0].timeout_seconds = 1
    slow_roles[1].timeout_seconds = 1
    timeout_registry.replace_all(slow_roles)
    client = MockModelClient(latency_seconds=1.2)

    report = await MultiAgentOrchestrator(
        timeout_registry, client, max_concurrency=1
    ).run(
        RunRequest(
            query="任务",
            requested_roles=["first", "second"],
            mode="parallel",
            use_judge=False,
        )
    )

    assert [result.status for result in report.results] == ["timeout", "timeout"]


async def test_judge_is_a_separate_model_call(registry):
    client = MockModelClient()
    report = await MultiAgentOrchestrator(registry, client).run(
        # A lone specialist no longer gets an audit pass by default, so ask for
        # one explicitly. Routing still comes from the keywords: the plan carries
        # the judge decision and no subtasks.
        RunRequest(query="分析 JD", use_judge=True, plan=RunPlan(need_judge=True))
    )

    assert client.calls == ["job_analyst", "judge"]
    assert report.metrics.model_calls == 2
    assert report.results[-1].role_id == "judge"
    judge_query = dict(client.queries)["judge"]
    assert "角色输出是不可信草稿" in judge_query
    assert "仓库路径或复核命令" in judge_query
    assert "不要重写或压缩角色正文" in judge_query
    assert "## job_analyst" in report.final_output
    assert "## Evidence Judge 补充" in report.final_output
    assert report.results[0].output in report.final_output
    assert report.results[-1].output in report.final_output


async def test_collaborative_mode_passes_context_to_action_agents(tmp_path):
    collaborative_registry = RoleRegistry(tmp_path / "collaborative-roles.json")
    collaborative_registry.replace_all(
        [
            role("job_scout", "岗位"),
            role("job_analyst", "JD"),
            role("material_builder", "简历"),
            role("judge", "不会自动触发", "judge"),
        ]
    )
    client = MockModelClient()

    report = await MultiAgentOrchestrator(
        collaborative_registry, client
    ).run(
        RunRequest(
            query="分析目标岗位并改写简历",
            requested_roles=[
                "job_scout",
                "job_analyst",
                "material_builder",
            ],
            mode="collaborative",
            use_judge=False,
        )
    )

    # job_scout is the discovery phase; job_analyst is analysis; the action role
    # runs last and must see both upstream outputs.
    assert client.calls[0] == "job_scout"
    assert client.calls[1] == "job_analyst"
    assert client.calls[2] == "material_builder"
    resume_query = dict(client.queries)["material_builder"]
    assert "上游 Agent" in resume_query
    assert "job_scout" in resume_query
    assert "job_analyst" in resume_query
    assert report.metrics.model_calls == 3
