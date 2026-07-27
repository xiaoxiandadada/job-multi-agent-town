import pytest

from job_agent_harness.commands import parse_run_command


def test_job_preset_selects_context_team():
    command = parse_run_command("/job 分析 AI Agent 岗位")

    assert command.query == "分析 AI Agent 岗位"
    assert command.requested_roles == [
        "job_scout",
        "jd_analyst",
        "job_knowledge_curator",
    ]
    assert command.mode == "collaborative"


def test_knowledge_preset_selects_single_role():
    command = parse_run_command("/knowledge 补充 RAG 评测知识")

    assert command.requested_roles == ["job_knowledge_curator"]
    assert command.mode == "single"


def test_agent_command_selects_named_role():
    command = parse_run_command("/agent resume_strategist 改写项目经历")

    assert command.query == "改写项目经历"
    assert command.requested_roles == ["resume_strategist"]
    assert command.mode == "single"


def test_ask_command_is_an_alias_for_a_specific_role():
    command = parse_run_command("/ask job_knowledge_curator 什么是 Agent 评测")

    assert command.query == "什么是 Agent 评测"
    assert command.requested_roles == ["job_knowledge_curator"]
    assert command.mode == "single"


@pytest.mark.parametrize(
    "text",
    ["/job", "/knowledge ", "/agent jd_analyst", "/ask jd_analyst"],
)
def test_command_without_query_is_rejected(text):
    with pytest.raises(ValueError, match="用法"):
        parse_run_command(text)
