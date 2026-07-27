from job_agent_harness.langgraph_orchestrator import LangGraphOrchestrator
from job_agent_harness.model_client import MockModelClient
from job_agent_harness.models import RoleSpec, RunRequest
from job_agent_harness.orchestrator import MultiAgentOrchestrator
from job_agent_harness.registry import RoleRegistry


def make_role(role_id, profile="default"):
    return RoleSpec(
        role_id=role_id,
        display_name=role_id,
        goal=f"完成 {role_id} 的求职任务",
        system_prompt=f"你是 {role_id}，只输出可验证内容。",
        trigger_keywords=[role_id],
        model_profile=profile,
    )


async def test_langgraph_adapter_preserves_two_phase_collaboration(tmp_path):
    registry = RoleRegistry(tmp_path / "roles.json")
    registry.replace_all(
        [
            make_role("jd_analyst"),
            make_role("job_knowledge_curator"),
            make_role("resume_strategist"),
            make_role("judge", "judge"),
        ]
    )
    client = MockModelClient()
    graph = LangGraphOrchestrator(
        MultiAgentOrchestrator(registry, client)
    )

    report = await graph.run(
        RunRequest(
            query="分析目标岗位并改写简历",
            requested_roles=[
                "jd_analyst",
                "job_knowledge_curator",
                "resume_strategist",
            ],
            mode="collaborative",
        ),
        thread_id="test-thread",
    )

    assert set(client.calls[:2]) == {"jd_analyst", "job_knowledge_curator"}
    assert client.calls[2:] == ["resume_strategist", "judge"]
    assert report.metrics.model_calls == 4
    assert report.run_id == "test-thread"
    assert "route" in graph.mermaid()
    assert "context_phase" in graph.mermaid()


async def test_langgraph_parallel_mode_does_not_add_stage_dependency(tmp_path):
    registry = RoleRegistry(tmp_path / "parallel-roles.json")
    registry.replace_all(
        [
            make_role("jd_analyst"),
            make_role("resume_strategist"),
            make_role("judge", "judge"),
        ]
    )
    client = MockModelClient()
    graph = LangGraphOrchestrator(
        MultiAgentOrchestrator(registry, client)
    )

    report = await graph.run(
        RunRequest(
            query="同时分析 JD 和简历",
            requested_roles=["jd_analyst", "resume_strategist"],
            mode="parallel",
            use_judge=False,
        )
    )

    assert set(client.calls) == {"jd_analyst", "resume_strategist"}
    assert report.metrics.model_calls == 2
