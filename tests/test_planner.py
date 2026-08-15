"""The decomposition step: what it produces, and what it does when it fails.

The planner replaced keyword routing, so these tests care about two things in
equal measure — that a good plan actually narrows the run, and that a bad one
degrades to the old router instead of taking the run down.
"""

import json

import pytest

from job_agent_harness.activity import ActivityStore
from job_agent_harness.chief_of_staff import (
    CHIEF_DISPLAY_NAME,
    ChiefOfStaff,
    dispatch_note,
    extract_json_object,
    keyword_plan,
    parse_plan,
    planner_candidates,
    planner_enabled,
    planner_prompt,
)
from job_agent_harness.model_client import MockModelClient
from job_agent_harness.models import (
    MAX_PLANNED_SUBTASKS,
    PlannedSubtask,
    RoleSpec,
    RunPlan,
    RunRequest,
)
from job_agent_harness.orchestrator import (
    MultiAgentOrchestrator,
    judge_needed,
    judge_needed_for,
)
from job_agent_harness.registry import RoleRegistry
from job_agent_harness.tasks import TaskGraphStore, build_run_task_graph


ROLE_IDS = {"job_scout", "job_analyst", "material_builder"}


def make_role(role_id: str, keyword: str, display_name: str = "") -> RoleSpec:
    return RoleSpec(
        role_id=role_id,
        display_name=display_name or role_id,
        goal=f"完成 {keyword} 相关的求职任务",
        system_prompt=f"你负责 {keyword}，只输出可核验证据。",
        trigger_keywords=[keyword],
    )


def plan_json(*role_ids: str, need_judge: bool = True, **extra) -> str:
    return json.dumps(
        {
            "intent": "改简历",
            "subtasks": [
                {
                    "role_id": role_id,
                    "task": f"{role_id} 要做的事",
                    "why": f"{role_id} 的理由",
                    "depends_on": [],
                }
                for role_id in role_ids
            ],
            "need_judge": need_judge,
            "skipped": "其他角色与本次问题无关",
            **extra,
        },
        ensure_ascii=False,
    )


@pytest.fixture
def parts(tmp_path):
    registry = RoleRegistry(tmp_path / "roles.json")
    registry.replace_all(
        [
            make_role("job_scout", "岗位", "Job Scout"),
            make_role("job_analyst", "JD", "Job Analyst"),
            make_role("material_builder", "简历", "Material Builder"),
            make_role("judge", "不会自动触发", "Evidence Judge"),
        ]
    )
    activity = ActivityStore(tmp_path / "activity.jsonl", excerpt_chars=600)
    graphs = TaskGraphStore(tmp_path / "graphs")
    return registry, activity, graphs


def build(parts, plan_reply=None):
    registry, activity, graphs = parts
    client = MockModelClient(plan_reply=plan_reply)
    orchestrator = MultiAgentOrchestrator(
        registry,
        client,
        activity_store=activity,
        task_graph_store=graphs,
    )
    return orchestrator, client, activity, graphs


def only(activity: ActivityStore, kind: str):
    found = [event for event in activity.read(limit=200) if event.kind == kind]
    assert len(found) == 1, f"{kind}: {len(found)} 条"
    return found[0]


# --------------------------------------------------------------- JSON parsing


def test_extract_json_object_reads_a_bare_object():
    assert extract_json_object('{"a": 1}') == {"a": 1}


def test_extract_json_object_unwraps_a_fenced_block():
    text = 'Sure!\n```json\n{"a": 1}\n```\n'

    assert extract_json_object(text) == {"a": 1}


def test_extract_json_object_survives_a_trailing_sentence():
    # A first-brace-to-last-brace slice would swallow the trailing prose and
    # fail to parse; brace balancing stops at the real end of the object.
    text = '{"a": {"b": 2}} 希望这个拆解可以（如果不对请告诉我）。'

    assert extract_json_object(text) == {"a": {"b": 2}}


def test_extract_json_object_ignores_braces_inside_strings():
    text = '{"task": "输出 {公司} 和 {岗位}"}'

    assert extract_json_object(text) == {"task": "输出 {公司} 和 {岗位}"}


def test_extract_json_object_returns_none_for_prose():
    assert extract_json_object("我建议先让 Job Scout 找岗位，然后……") is None


# ------------------------------------------------------------- plan parsing


def test_parse_plan_keeps_what_the_planner_chose():
    plan = parse_plan(plan_json("job_analyst", need_judge=False), ROLE_IDS)

    assert plan is not None
    assert plan.source == "planner"
    assert plan.role_ids() == ["job_analyst"]
    assert plan.subtasks[0].task == "job_analyst 要做的事"
    assert plan.need_judge is False
    assert plan.skipped


def test_parse_plan_drops_a_hallucinated_role():
    # An unknown id would raise KeyError deep inside the orchestrator.
    plan = parse_plan(plan_json("job_analyst", "salary_negotiator"), ROLE_IDS)

    assert plan is not None
    assert plan.role_ids() == ["job_analyst"]


def test_parse_plan_enforces_the_subtask_ceiling():
    plan = parse_plan(
        plan_json("job_scout", "job_analyst", "material_builder", "judge"),
        ROLE_IDS | {"judge"},
    )

    assert plan is not None
    assert len(plan.subtasks) == MAX_PLANNED_SUBTASKS


def test_parse_plan_deduplicates_a_repeated_role():
    plan = parse_plan(plan_json("job_analyst", "job_analyst"), ROLE_IDS)

    assert plan is not None
    assert plan.role_ids() == ["job_analyst"]


def test_parse_plan_drops_dependencies_on_roles_that_were_cut():
    # material_builder depends on a role the ceiling removed; keeping the edge
    # would leave its task node blocked forever.
    payload = json.loads(
        plan_json("job_scout", "job_analyst", "material_builder")
    )
    payload["subtasks"].append(
        {
            "role_id": "interview_coach",
            "task": "第四个，会被截断",
            "why": "",
            "depends_on": [],
        }
    )
    payload["subtasks"][2]["depends_on"] = ["interview_coach", "job_scout"]

    plan = parse_plan(
        json.dumps(payload, ensure_ascii=False),
        ROLE_IDS | {"interview_coach"},
    )

    assert plan is not None
    assert plan.role_ids() == ["job_scout", "job_analyst", "material_builder"]
    assert plan.subtasks[2].depends_on == ["job_scout"]


def test_parse_plan_rejects_a_self_dependency():
    payload = json.loads(plan_json("job_analyst"))
    payload["subtasks"][0]["depends_on"] = ["job_analyst"]

    plan = parse_plan(json.dumps(payload, ensure_ascii=False), ROLE_IDS)

    assert plan is not None
    assert plan.subtasks[0].depends_on == []


def test_parse_plan_fills_in_a_missing_task_description():
    payload = json.loads(plan_json("job_analyst"))
    payload["subtasks"][0]["task"] = ""

    plan = parse_plan(json.dumps(payload, ensure_ascii=False), ROLE_IDS)

    # The task text becomes a task-graph node title; a blank card is worse than
    # a generic one.
    assert plan is not None
    assert plan.subtasks[0].task == "完成该角色在本次任务中的交付"


@pytest.mark.parametrize(
    "reply",
    [
        "我建议派 Job Analyst 去分析。",
        '{"intent": "改简历"}',
        '{"intent": "x", "subtasks": []}',
        '{"intent": "x", "subtasks": "job_analyst"}',
        # A bare list of subtasks: the first object gets extracted, has no
        # "subtasks" key, and must not be mistaken for a plan.
        '[{"role_id": "job_analyst", "task": "分析"}]',
        plan_json("salary_negotiator"),
        "",
    ],
)
def test_parse_plan_returns_none_when_there_is_nothing_usable(reply):
    # None is the signal to fall back; a plan with zero roles would run nobody.
    assert parse_plan(reply, ROLE_IDS) is None


# ------------------------------------------------------------------- prompt


def test_planner_candidates_offers_id_name_and_goal_only():
    menu = planner_candidates([make_role("job_analyst", "JD", "Job Analyst")])

    assert menu == "- job_analyst（Job Analyst）：完成 JD 相关的求职任务"
    # Not the 4000-character system prompt: the planner picks who, and paying
    # for every role's full contract would cost more than the run it shrinks.
    assert "只输出可核验证据" not in menu


def test_planner_prompt_mentions_attachments_when_there_are_any():
    roles = [make_role("job_analyst", "JD")]

    assert "2 张图片" in planner_prompt("看这个", roles, image_count=2)
    assert "图片" not in planner_prompt("看这个", roles)


def test_planner_flag_is_on_unless_explicitly_turned_off():
    assert planner_enabled({}) is True
    for value in ("0", "false", "off", "no", " OFF "):
        assert planner_enabled({"JOB_AGENT_CHIEF_PLANNER": value}) is False


# ------------------------------------------------------- routing precedence


async def test_the_plan_decides_who_runs_not_the_keywords(parts):
    orchestrator, client, activity, _ = build(
        parts,
        plan_reply=plan_json("material_builder", need_judge=False),
    )

    # Every keyword in this query points at job_scout and job_analyst. The old
    # router would have woken both; the plan names neither.
    report = await ChiefOfStaff(orchestrator, with_closing=False).run(
        RunRequest(query="这个岗位的 JD 要求是什么")
    )

    assert [result.role_id for result in report.results] == ["material_builder"]
    plan_event = only(activity, "plan_created")
    assert plan_event.status == "ok"
    assert plan_event.selected_role_ids == ["material_builder"]
    assert plan_event.metrics["subtask_count"] == 1


async def test_an_unparseable_plan_falls_back_to_keyword_routing(parts):
    orchestrator, client, activity, _ = build(
        parts,
        plan_reply="我觉得应该先分析 JD。",
    )

    report = await ChiefOfStaff(orchestrator, with_closing=False).run(
        RunRequest(query="这个岗位的 JD 要求是什么", use_judge=False)
    )

    # The run survives, and the timeline says which router actually decided.
    assert {result.role_id for result in report.results} == {
        "job_scout",
        "job_analyst",
    }
    plan_event = only(activity, "plan_created")
    assert plan_event.status == "error"
    assert "关键词路由" in (plan_event.error or "")
    assert only(activity, "intake_completed").metrics["plan_source"] == "keywords"


async def test_a_planner_that_raises_does_not_take_the_run_down(parts):
    orchestrator, client, activity, _ = build(parts)

    async def boom(role, query, *, images=()):
        if MockModelClient.planner_marker in query:
            raise TimeoutError("planner timed out")
        return await MockModelClient.complete(client, role, query, images=images)

    orchestrator.model_client = type(
        "Flaky",
        (),
        {
            "complete": staticmethod(boom),
            "model_for": staticmethod(lambda role: "mock"),
        },
    )()

    report = await ChiefOfStaff(orchestrator, with_closing=False).run(
        RunRequest(query="这个岗位的 JD 要求是什么", use_judge=False)
    )

    assert report.final_output
    plan_event = only(activity, "plan_created")
    assert plan_event.status == "error"
    assert "planner timed out" in (plan_event.error or "")


async def test_an_explicit_role_request_skips_the_planner_entirely(parts):
    orchestrator, client, activity, _ = build(parts)

    report = await ChiefOfStaff(orchestrator, with_closing=False).run(
        RunRequest(
            query="改我的简历",
            requested_roles=["material_builder"],
            mode="single",
            use_judge=False,
        )
    )

    # A Feishu role bot has already answered "who"; paying for a plan would be
    # a call spent to reach the conclusion the caller handed in.
    assert "plan_created" not in [
        event.kind for event in activity.read(limit=200)
    ]
    assert client.calls == ["material_builder"]
    assert [result.role_id for result in report.results] == ["material_builder"]


async def test_the_planner_can_be_turned_off(parts):
    orchestrator, client, activity, _ = build(parts)

    await ChiefOfStaff(
        orchestrator,
        with_closing=False,
        with_planner=False,
    ).run(RunRequest(query="这个岗位的 JD 要求是什么", use_judge=False))

    assert "plan_created" not in [
        event.kind for event in activity.read(limit=200)
    ]
    assert client.calls == ["job_scout", "job_analyst"]


async def test_a_precomputed_plan_is_not_planned_again(parts):
    orchestrator, client, activity, _ = build(parts)
    plan = RunPlan(
        subtasks=[PlannedSubtask(role_id="job_analyst", task="分析这份 JD")],
        need_judge=False,
    )

    report = await ChiefOfStaff(orchestrator, with_closing=False).run(
        RunRequest(query="随便什么问题", plan=plan)
    )

    assert "plan_created" not in [
        event.kind for event in activity.read(limit=200)
    ]
    assert [result.role_id for result in report.results] == ["job_analyst"]


async def test_the_plan_survives_the_attachment_rewrite(parts):
    orchestrator, client, activity, _ = build(
        parts,
        plan_reply=plan_json("job_scout", "job_analyst"),
    )

    report = await ChiefOfStaff(orchestrator, with_closing=False).run(
        RunRequest(
            query="看看这张 JD 截图",
            use_judge=False,
            images=[
                {
                    "media_type": "image/png",
                    "data": "aGVsbG8td29ybGQ=",
                    "source_name": "jd.png",
                }
            ],
        )
    )

    # Reading the picture rebuilds the request. Rebuilding it from the original
    # would drop the plan and silently re-route by keyword.
    assert {result.role_id for result in report.results} == {
        "job_scout",
        "job_analyst",
    }
    assert only(activity, "intake_completed").metrics["plan_source"] == "planner"


async def test_a_dependency_upgrades_the_run_to_collaborative(parts):
    orchestrator, client, activity, _ = build(parts)
    plan = RunPlan(
        subtasks=[
            PlannedSubtask(role_id="job_scout", task="找到这个岗位"),
            PlannedSubtask(
                role_id="job_analyst",
                task="基于上游证据拆解要求",
                depends_on=["job_scout"],
            ),
        ],
        need_judge=False,
    )

    report = await ChiefOfStaff(orchestrator, with_closing=False).run(
        RunRequest(query="字节这个岗位适合我吗", mode="parallel", plan=plan)
    )

    # Parallel would have run both at once and handed job_analyst nothing, making
    # the planner's dependency a note nobody acted on.
    assert report.mode == "collaborative"
    analyst_query = next(
        query for role_id, query in client.queries if role_id == "job_analyst"
    )
    assert "Job Scout" in analyst_query


async def test_a_plan_without_dependencies_stays_parallel(parts):
    orchestrator, _, _, _ = build(parts)
    plan = RunPlan(
        subtasks=[
            PlannedSubtask(role_id="job_scout", task="找岗位"),
            PlannedSubtask(role_id="material_builder", task="改简历"),
        ],
        need_judge=False,
    )

    report = await ChiefOfStaff(orchestrator, with_closing=False).run(
        RunRequest(query="找岗位并改简历", mode="parallel", plan=plan)
    )

    # Independent work should not pay for phase barriers.
    assert report.mode == "parallel"


# ------------------------------------------------------------------ receipt


def test_the_receipt_quotes_the_planners_reasoning():
    roles = [make_role("job_analyst", "JD", "Job Analyst")]
    plan = RunPlan(
        intent="搞清楚这个岗位要什么",
        subtasks=[
            PlannedSubtask(
                role_id="job_analyst",
                task="拆解硬要求",
                why="用户已经给了 JD 原文",
            )
        ],
        need_judge=False,
        skipped="没派 Job Scout，因为岗位已经确定",
    )

    note = dispatch_note(
        "这个岗位的 JD 要求是什么",
        roles,
        mode="single",
        use_judge=False,
        with_closing=False,
        plan=plan,
    )

    assert note.splitlines() == [
        f"📋 {CHIEF_DISPLAY_NAME} 已接单",
        "- 意图：搞清楚这个岗位要什么",
        "- 附件：无",
        "- 分派：Job Analyst（单角色）",
        "- 拆解：Job Analyst←用户已经给了 JD 原文",
        "- 未派：没派 Job Scout，因为岗位已经确定",
        "- 收尾：直接返回角色原文",
    ]


def test_the_receipt_still_names_keywords_without_a_planner_plan():
    roles = [make_role("job_analyst", "JD", "Job Analyst")]

    note = dispatch_note(
        "看看这个 JD",
        roles,
        mode="single",
        use_judge=False,
        with_closing=False,
        plan=keyword_plan(roles, use_judge=False),
    )

    # A keyword-sourced plan must not be dressed up as a decomposition.
    assert "- 依据：Job Analyst←JD" in note
    assert "拆解" not in note


# -------------------------------------------------------------- judge gating


def test_judge_runs_whenever_two_specialists_have_to_be_reconciled():
    request = RunRequest(query="一个问题", plan=RunPlan(need_judge=False))

    assert judge_needed(request, ["a", "b"]) is True


def test_judge_is_skipped_for_a_single_answer_the_planner_trusts():
    request = RunRequest(
        query="一个问题",
        plan=RunPlan(
            subtasks=[PlannedSubtask(role_id="job_analyst", task="做这件事")],
            need_judge=False,
        ),
    )

    assert judge_needed(request, ["a"]) is False


def test_judge_still_runs_on_one_answer_the_planner_flagged():
    request = RunRequest(
        query="一个问题",
        plan=RunPlan(
            subtasks=[PlannedSubtask(role_id="job_scout", task="做这件事")],
            need_judge=True,
        ),
    )

    assert judge_needed(request, ["a"]) is True


def test_judge_is_skipped_for_a_lone_planless_role():
    # The latency the caller asked to remove: a single specialist answering a
    # direct question only ever got an appended audit note.
    assert judge_needed(RunRequest(query="一个问题"), ["a"]) is False


def test_use_judge_false_always_wins():
    request = RunRequest(query="一个问题", use_judge=False, plan=RunPlan(need_judge=True))

    assert judge_needed(request, ["a", "b"]) is False
    assert judge_needed_for(request) is False


def test_nothing_succeeded_means_nothing_to_audit():
    assert judge_needed(RunRequest(query="一个问题"), []) is False


# ----------------------------------------------------------------- task graph


def test_the_task_graph_uses_the_planners_own_task_text():
    roles = [make_role("job_analyst", "JD", "Job Analyst")]
    plan = RunPlan(
        intent="搞清楚这个岗位要什么",
        subtasks=[
            PlannedSubtask(
                role_id="job_analyst",
                task="拆解硬要求与加分项",
                why="用户已给出 JD 原文",
            )
        ],
        need_judge=False,
    )

    graph = build_run_task_graph(
        run_id="r1",
        query="这个岗位的 JD 要求是什么",
        roles=roles,
        mode="parallel",
        use_judge=False,
        plan=plan,
    )

    assert graph.title == "搞清楚这个岗位要什么"
    assert graph.tasks[0].title == "Job Analyst：拆解硬要求与加分项"
    assert "用户已给出 JD 原文" in graph.tasks[0].description


def test_the_task_graph_keeps_the_role_goal_without_a_planner_plan():
    roles = [make_role("job_analyst", "JD", "Job Analyst")]

    graph = build_run_task_graph(
        run_id="r1",
        query="q",
        roles=roles,
        mode="parallel",
        use_judge=False,
        plan=keyword_plan(roles),
    )

    assert graph.title == "LangGraph 求职协作任务图"
    assert graph.tasks[0].title == "Job Analyst：完成 JD 相关的求职任务"


def test_the_graph_only_draws_dependencies_the_run_will_honour():
    scout = make_role("job_scout", "岗位", "Job Scout")
    analyst = make_role("job_analyst", "JD", "Job Analyst")
    scout.workflow_stage = "context"
    analyst.workflow_stage = "context"
    plan = RunPlan(
        subtasks=[
            PlannedSubtask(role_id="job_scout", task="找岗位"),
            PlannedSubtask(
                role_id="job_analyst",
                task="分析 JD",
                depends_on=["job_scout"],
            ),
        ]
    )

    graph = build_run_task_graph(
        run_id="r1",
        query="q",
        roles=[scout, analyst],
        mode="parallel",
        use_judge=False,
        plan=plan,
    )

    # Both roles run in the same parallel batch, so the planner's edge between
    # them is not something the run observes. Drawing it would tell the user
    # job_analyst waited for job_scout when it did not.
    assert {task.task_id: task.depends_on for task in graph.tasks} == {
        "job_scout": [],
        "job_analyst": [],
    }
