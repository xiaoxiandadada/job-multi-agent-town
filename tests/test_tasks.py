from job_agent_harness.models import RoleSpec
from job_agent_harness.tasks import (
    TaskGraphStore,
    build_run_task_graph,
)


def role(role_id: str, stage: str) -> RoleSpec:
    return RoleSpec(
        role_id=role_id,
        display_name=role_id,
        goal=f"完成 {role_id} 的专业求职交付",
        system_prompt=f"只输出 {role_id} 的可核验证据。",
        workflow_stage=stage,
    )


def test_collaborative_task_graph_exposes_real_dependencies_and_progress(
    tmp_path,
):
    graph = build_run_task_graph(
        run_id="task-run",
        query="分析新岗位并生成求职行动",
        roles=[
            role("job_scout", "context"),
            role("job_analyst", "context"),
            role("material_builder", "action"),
            role("interview_coach", "action"),
        ],
        mode="collaborative",
        use_judge=True,
    )
    store = TaskGraphStore(tmp_path / "task_graphs")
    graph = store.create(graph)
    by_id = {task.task_id: task for task in graph.tasks}

    # Three layers: discovery → analysis → action, then the judge over all of it.
    assert by_id["job_scout"].phase == "discovery"
    assert by_id["job_scout"].status == "ready"
    assert by_id["job_analyst"].phase == "analysis"
    assert by_id["job_analyst"].depends_on == ["job_scout"]
    assert by_id["material_builder"].depends_on == [
        "job_scout",
        "job_analyst",
    ]
    assert by_id["interview_coach"].depends_on == [
        "job_scout",
        "job_analyst",
    ]
    assert by_id["judge"].depends_on == [
        "job_scout",
        "job_analyst",
        "material_builder",
        "interview_coach",
    ]

    store.update_task("task-run", "job_scout", status="completed")
    graph = store.get("task-run")
    by_id = {task.task_id: task for task in graph.tasks}
    assert by_id["job_analyst"].status == "ready"
    # The action roles wait for analysis, not just for discovery.
    assert by_id["material_builder"].status == "blocked"
    assert by_id["interview_coach"].status == "blocked"

    store.update_task("task-run", "job_analyst", status="completed")
    graph = store.get("task-run")
    by_id = {task.task_id: task for task in graph.tasks}
    assert by_id["material_builder"].status == "ready"
    assert by_id["interview_coach"].status == "ready"
    assert graph.progress > 0
