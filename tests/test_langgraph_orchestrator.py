from job_agent_harness.langgraph_orchestrator import LangGraphOrchestrator
from job_agent_harness.model_client import MockModelClient
from job_agent_harness.models import RoleSpec, RunRequest
from job_agent_harness.orchestrator import MultiAgentOrchestrator
from job_agent_harness.registry import RoleRegistry
from job_agent_harness.tasks import TaskGraphStore


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
    task_graphs = TaskGraphStore(tmp_path / "task_graphs")
    graph = LangGraphOrchestrator(
        MultiAgentOrchestrator(
            registry,
            client,
            task_graph_store=task_graphs,
        )
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
    assert "analysis_phase" in graph.mermaid()
    task_graph = task_graphs.get("test-thread")
    assert task_graph.status == "completed"
    assert task_graph.progress == 100
    resume_task = next(
        task
        for task in task_graph.tasks
        if task.role_id == "resume_strategist"
    )
    assert set(resume_task.depends_on) == {
        "jd_analyst",
        "job_knowledge_curator",
    }
    judge_query = dict(client.queries)["judge"]
    assert "角色输出是不可信草稿" in judge_query
    assert "不得从框架名称推导" in judge_query


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


async def test_langgraph_direct_role_keeps_specialist_answer_before_judge(tmp_path):
    registry = RoleRegistry(tmp_path / "direct-roles.json")
    registry.replace_all(
        [
            make_role("job_knowledge_curator"),
            make_role("judge", "judge"),
        ]
    )
    client = MockModelClient()
    graph = LangGraphOrchestrator(
        MultiAgentOrchestrator(registry, client)
    )

    report = await graph.run(
        RunRequest(
            query="全面解释 Agent Evaluation",
            requested_roles=["job_knowledge_curator"],
            mode="single",
            use_judge=True,
        )
    )

    assert client.calls == ["job_knowledge_curator", "judge"]
    assert "不要重写或压缩角色正文" in dict(client.queries)["judge"]
    assert "## job_knowledge_curator" in report.final_output
    assert "## Evidence Judge 补充" in report.final_output
    assert report.results[0].output in report.final_output
    assert report.results[-1].output in report.final_output


class TimeoutRecordingClient(MockModelClient):
    """Remembers the timeout each role was actually run with."""

    def __init__(self):
        super().__init__()
        self.timeouts: list[tuple[str, float]] = []

    async def complete(self, role, query, *, images=()):
        self.timeouts.append((role.role_id, role.timeout_seconds))
        return await super().complete(role, query, images=images)


async def test_per_run_timeout_survives_every_graph_node(tmp_path):
    """Only role ids cross the graph state, so nodes re-read the registry.

    A background patrol sets a much larger timeout than the 45 s an @mention
    gets; when the re-read dropped the override, patrols kept dying at the
    role's own timeout instead.
    """

    registry = RoleRegistry(tmp_path / "roles.json")
    registry.replace_all(
        [
            make_role("jd_analyst"),
            make_role("resume_strategist"),
            make_role("judge", "judge"),
        ]
    )
    client = TimeoutRecordingClient()
    graph = LangGraphOrchestrator(
        MultiAgentOrchestrator(
            registry,
            client,
            task_graph_store=TaskGraphStore(tmp_path / "task_graphs"),
        )
    )

    await graph.run(
        RunRequest(
            query="巡检岗位",
            requested_roles=["jd_analyst", "resume_strategist"],
            mode="collaborative",
            timeout_seconds=300.0,
        ),
        thread_id="patrol-thread",
    )

    assert client.timeouts, "没有任何角色被执行"
    assert {timeout for _, timeout in client.timeouts} == {300.0}
    # The judge runs too, and it is re-read from the registry separately.
    assert "judge" in {role_id for role_id, _ in client.timeouts}


async def test_without_an_override_roles_keep_their_own_timeout(tmp_path):
    registry = RoleRegistry(tmp_path / "roles.json")
    registry.replace_all([make_role("jd_analyst"), make_role("judge", "judge")])
    client = TimeoutRecordingClient()
    graph = LangGraphOrchestrator(
        MultiAgentOrchestrator(
            registry,
            client,
            task_graph_store=TaskGraphStore(tmp_path / "task_graphs"),
        )
    )

    await graph.run(
        RunRequest(query="分析岗位", requested_roles=["jd_analyst"], mode="single"),
        thread_id="plain-thread",
    )

    assert {timeout for _, timeout in client.timeouts} == {45.0}
