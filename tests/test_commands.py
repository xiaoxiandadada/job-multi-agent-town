import pytest

from job_agent_harness.commands import (
    DAILY_FLOW_TASK,
    FLOW_TIMEOUT_SECONDS,
    looks_like_daily_flow,
    parse_run_command,
)


def test_today_runs_the_whole_flow_with_no_argument():
    """The point of the command: the daily routine needs no retyping.

    Every other preset requires a task, which is what made "run today's flow" a
    thing you had to compose by hand every morning.
    """

    command = parse_run_command("/today")

    assert command.query == DAILY_FLOW_TASK
    assert command.requested_roles == [
        "job_scout",
        "job_analyst",
        "match_scorer",
        "material_builder",
        "interview_coach",
    ]
    assert command.mode == "collaborative"
    assert command.label == "今日求职流程"


def test_today_appends_a_steer_instead_of_replacing_the_flow():
    command = parse_run_command("/today 重点看字节的岗位")

    assert command.query.startswith(DAILY_FLOW_TASK)
    assert "重点看字节的岗位" in command.query
    # Every stage still runs — the steer narrows them, it does not replace them.
    assert len(command.requested_roles) == 5


def test_a_plain_sentence_can_reach_the_standing_flow():
    """The flow needs five roles; the planner caps itself at three.

    So decomposition cannot reach the full flow however well it reads the
    sentence — this alias is the only path to it short of typing the command.
    """

    command = parse_run_command("做一下今日的求职流程")

    assert command.requested_roles == [
        "job_scout",
        "job_analyst",
        "match_scorer",
        "material_builder",
        "interview_coach",
    ]
    assert command.query.startswith(DAILY_FLOW_TASK)
    assert "用户原话：做一下今日的求职流程" in command.query


@pytest.mark.parametrize(
    "text",
    [
        "做一下今日的求职流程",
        "今天的求职流程跑一下",
        "帮我走一遍今日流程",
        "把整套流程过一遍",
        "跑一次全流程",
    ],
)
def test_flow_phrasings_that_should_match(text):
    assert looks_like_daily_flow(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "今天投了三家，帮我看看简历",
        "分析这个岗位的 JD",
        "今天该复习什么",
        # A bare 流程 must not match: this is a question about the company, and
        # the planner answers it with one role.
        "这个岗位的面试流程是几轮",
        "投递流程是什么样的",
    ],
)
def test_sentences_that_must_still_reach_the_planner(text):
    # A keyword rule that swallowed these would undo the whole point of having a
    # planner: the planner picks 1-2 roles for them, the flow would run four.
    assert looks_like_daily_flow(text) is False
    assert parse_run_command(text).requested_roles == []
    assert parse_run_command(text).mode == "parallel"


def test_presets_that_need_a_task_still_reject_an_empty_one():
    for command in ("/job", "/apply", "/interview", "/team", "/knowledge"):
        with pytest.raises(ValueError, match="用法"):
            parse_run_command(command)


def test_the_standing_flow_gets_a_wider_timeout_than_a_single_question():
    """An action-stage role's input grows with the run; its timeout must too.

    Measured: a real /today run pushed 124k input tokens, and Interview Coach —
    last in the chain, so it receives every upstream role's full output — hit its
    170 s role timeout while the earlier stages finished.
    """

    assert parse_run_command("/today").timeout_seconds == FLOW_TIMEOUT_SECONDS
    assert (
        parse_run_command("做一下今日的求职流程").timeout_seconds
        == FLOW_TIMEOUT_SECONDS
    )
    # A one-off question keeps the role's own budget: nothing grew.
    assert parse_run_command("/job 分析这个岗位").timeout_seconds is None
    assert parse_run_command("随便问一句").timeout_seconds is None


def test_job_preset_selects_context_team():
    command = parse_run_command("/job 分析 AI Agent 岗位")

    assert command.query == "分析 AI Agent 岗位"
    assert command.requested_roles == ["job_scout", "job_analyst"]
    assert command.mode == "collaborative"


def test_knowledge_preset_selects_single_role():
    command = parse_run_command("/knowledge 补充 RAG 评测知识")

    assert command.requested_roles == ["job_analyst"]
    assert command.mode == "single"


def test_agent_command_selects_named_role():
    command = parse_run_command("/agent material_builder 改写项目经历")

    assert command.query == "改写项目经历"
    assert command.requested_roles == ["material_builder"]
    assert command.mode == "single"


def test_ask_command_is_an_alias_for_a_specific_role():
    command = parse_run_command("/ask job_analyst 什么是 Agent 评测")

    assert command.query == "什么是 Agent 评测"
    assert command.requested_roles == ["job_analyst"]
    assert command.mode == "single"


@pytest.mark.parametrize(
    "text",
    ["/job", "/knowledge ", "/agent job_analyst", "/ask job_analyst"],
)
def test_command_without_query_is_rejected(text):
    with pytest.raises(ValueError, match="用法"):
        parse_run_command(text)
