from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from job_agent_harness.interview_session import (
    DEFAULT_PLANNED_TURNS,
    InterviewCoach,
    InterviewSession,
    InterviewSessionStore,
    coach_role,
    parse_coach_reply,
    parse_interview_command,
    render_session_summary,
    session_is_stale,
)
from job_agent_harness.models import ModelReply, RoleSpec


COACH = RoleSpec(
    role_id="interview_coach",
    display_name="Interview Coach",
    goal="围绕目标 JD 生成问题、追问、评分标准和学习任务",
    system_prompt="你是 Interview Coach。固定输出：知识题、项目深挖题、评分 rubric。",
)


def make_registry(role: RoleSpec = COACH):
    return SimpleNamespace(get=lambda role_id: role)


class ScriptedModel:
    """A model whose replies are fixed, so the flow itself is under test."""

    def __init__(self, replies: list[str]):
        self.replies = list(replies)
        self.prompts: list[str] = []
        self.roles: list[RoleSpec] = []

    async def __call__(self, role, query):
        self.roles.append(role)
        self.prompts.append(query)
        reply = self.replies.pop(0) if self.replies else "评分：6/10\n点评：还行"
        return ModelReply(content=reply)


def make_coach(tmp_path, replies: list[str]) -> tuple[InterviewCoach, ScriptedModel]:
    model = ScriptedModel(replies)
    coach = InterviewCoach(
        registry=make_registry(),
        complete=model,
        store=InterviewSessionStore(tmp_path / "interviews"),
    )
    return coach, model


def test_a_plain_question_without_a_session_is_not_an_interview_answer():
    """Otherwise starting the coach bot breaks every other way of asking it."""

    assert parse_interview_command("帮我看看百度这个 JD", session_open=False) is None
    assert (
        parse_interview_command("我做过一个多 Agent 项目", session_open=True).action
        == "answer"
    )


def test_other_slash_commands_never_reach_the_interview():
    assert parse_interview_command("/daily", session_open=True) is None
    assert parse_interview_command("/roles", session_open=True) is None


def test_start_command_reads_topic_voice_and_rounds():
    command = parse_interview_command(
        "/mock start 百度 Agent应用全栈 J99974 语音 8轮",
        session_open=False,
    )

    assert command.action == "start"
    assert command.payload == "百度 Agent应用全栈 J99974"
    assert command.voice is True
    assert command.planned_turns == 8


def test_start_without_a_keyword_still_starts():
    command = parse_interview_command("/mock 拼多多 AI Agent", session_open=False)

    assert command.action == "start"
    assert command.payload == "拼多多 AI Agent"
    assert command.voice is None
    assert command.planned_turns is None


def test_bare_mock_asks_for_help_instead_of_a_nameless_interview():
    assert parse_interview_command("/mock", session_open=False).action == "help"


def test_parse_coach_reply_splits_score_feedback_and_next_question():
    reply = parse_coach_reply(
        "评分：7/10\n"
        "点评：你提到了 JD Radar 的评测集，但没说清 golden set 怎么标。\n"
        "更好的说法：先讲失败样本，再讲指标。\n"
        "下一题：你的 golden set 有多少条，标注一致性怎么保证？"
    )

    assert reply.score == 7.0
    assert "golden set 怎么标" in reply.feedback
    assert reply.question == "你的 golden set 有多少条，标注一致性怎么保证？"
    assert "下一题" not in reply.feedback


def test_a_reply_that_drifts_from_the_format_still_yields_feedback():
    reply = parse_coach_reply("这个回答偏抽象，建议先给指标再给结论。")

    assert reply.score is None
    assert reply.question == ""
    assert reply.feedback.startswith("这个回答偏抽象")


@pytest.mark.asyncio
async def test_a_full_session_keeps_the_transcript_between_messages(tmp_path):
    """Each Feishu callback is a fresh process-level event with no state."""

    coach, model = make_coach(
        tmp_path,
        [
            "开场：我是百度 Agent 岗面试官。\n第 1 题：讲一个你做过的多 Agent 项目。\n考察点：项目深度",
            "评分：7/10\n点评：证据具体。\n下一题：这套评测的 golden set 怎么构造？",
        ],
    )

    start = await coach.handle("oc_chat", "/mock start 百度 Agent 应用全栈 语音")
    answer = await coach.handle("oc_chat", "我做了 JD Radar，7 个角色协作。")

    assert "第 1 题" in start.markdown
    assert start.session.voice is True
    assert start.session.planned_turns == DEFAULT_PLANNED_TURNS
    assert answer.speech == "这套评测的 golden set 怎么构造？"
    # The second prompt must carry the first question and the real answer.
    assert "讲一个你做过的多 Agent 项目" in model.prompts[1]
    assert "JD Radar" in model.prompts[1]
    # And the score landed on the turn it belongs to, in the stored transcript.
    stored = coach.store.load("oc_chat")
    assert [turn.score for turn in stored.turns] == [7.0, None]
    assert stored.turns[0].answer == "我做了 JD Radar，7 个角色协作。"
    assert stored.open_turn.index == 2


@pytest.mark.asyncio
async def test_the_greeting_is_not_stored_as_the_first_question(tmp_path):
    """Otherwise the summary and the next prompt quote「你好，我是面试官」."""

    coach, model = make_coach(
        tmp_path,
        [
            "你好，我是今天的面试官，我们进行 3 轮模拟面试。\n"
            "第 1 题：为什么要做两套编排器？\n"
            "考察点：架构取舍",
            "评分：5/10\n点评：没答场景。\n下一题：失败怎么恢复？",
        ],
    )

    start = await coach.handle("oc_chat", "/mock start 百度 Agent应用全栈 3轮")
    await coach.handle("oc_chat", "我做了 7 个角色的系统。")

    # The greeting still reaches the user, it just isn't the question of record.
    assert "我是今天的面试官" in start.markdown
    stored = coach.store.load("oc_chat")
    assert stored.turns[0].question == "为什么要做两套编排器？\n考察点：架构取舍"
    assert "我是今天的面试官" not in render_session_summary(stored)
    assert "我是今天的面试官" not in model.prompts[1]


@pytest.mark.asyncio
async def test_the_live_mode_overrides_the_one_shot_question_pack_prompt(tmp_path):
    coach, model = make_coach(tmp_path, ["第 1 题：介绍一个项目。"])

    await coach.handle("oc_chat", "/mock start 拼多多 AI Agent")

    assert "一次只问一道题" in model.roles[0].system_prompt
    # The registry prompt is kept, not replaced — grounding rules still apply.
    assert model.roles[0].system_prompt.startswith("你是 Interview Coach。")
    assert model.roles[0].role_id == "interview_coach"


@pytest.mark.asyncio
async def test_a_voice_answer_is_marked_as_transcribed(tmp_path):
    coach, model = make_coach(
        tmp_path,
        ["第 1 题：讲讲你的评测集。", "评分：6/10\n点评：口述不错。\n下一题：延迟怎么测？"],
    )

    await coach.handle("oc_chat", "/mock start 百度 语音")
    await coach.handle("oc_chat", "我们用 30 条 golden set", spoken=True)

    assert "语音转写" in model.prompts[1]
    assert coach.store.load("oc_chat").turns[0].spoken is True


@pytest.mark.asyncio
async def test_the_last_round_ends_the_session_with_a_review(tmp_path):
    coach, _ = make_coach(
        tmp_path,
        [
            "第 1 题：讲一个项目。",
            "评分：8/10\n点评：很具体。\n收尾：本轮结束。",
            "总评：可以过一面。\n逐题得分：第 1 题 8 分。",
        ],
    )

    await coach.handle("oc_chat", "/mock start 百度 1轮")
    final = await coach.handle("oc_chat", "我做了 JD Radar。")

    assert final.finished is True
    assert "总评" in final.markdown
    assert "平均分：8.0/10" in final.markdown
    # A closed session must not swallow the next ordinary question.
    assert coach.store.load("oc_chat") is None


@pytest.mark.asyncio
async def test_end_still_reports_when_the_closing_model_call_fails(tmp_path):
    class Failing:
        calls = 0

        async def __call__(self, role, query):
            Failing.calls += 1
            if Failing.calls == 1:
                return ModelReply(content="第 1 题：讲讲评测。")
            raise RuntimeError("上游超时")

    coach = InterviewCoach(
        registry=make_registry(),
        complete=Failing(),
        store=InterviewSessionStore(tmp_path / "interviews"),
    )
    await coach.handle("oc_chat", "/mock start 百度")

    end = await coach.handle("oc_chat", "/mock end")

    assert end.finished is True
    assert "模拟面试记录" in end.markdown
    assert "RuntimeError" in end.markdown
    assert coach.store.load("oc_chat") is None


@pytest.mark.asyncio
async def test_starting_a_new_topic_closes_the_previous_session(tmp_path):
    coach, _ = make_coach(
        tmp_path,
        ["第 1 题：A 主题的题。", "第 1 题：B 主题的题。"],
    )

    await coach.handle("oc_chat", "/mock start 百度 Agent")
    await coach.handle("oc_chat", "/mock start 拼多多 数据分析")

    session = coach.store.load("oc_chat")
    assert session.topic == "拼多多 数据分析"
    assert len(session.turns) == 1


@pytest.mark.asyncio
async def test_two_chats_do_not_share_one_transcript(tmp_path):
    coach, _ = make_coach(
        tmp_path,
        ["第 1 题：群里的题。", "第 1 题：私聊的题。"],
    )

    await coach.handle("oc_group", "/mock start 百度")
    await coach.handle("oc_private", "/mock start 拼多多")

    assert coach.store.load("oc_group").topic == "百度"
    assert coach.store.load("oc_private").topic == "拼多多"


def test_an_abandoned_session_stops_claiming_new_messages(tmp_path):
    store = InterviewSessionStore(tmp_path / "interviews")
    stale = InterviewSession(
        chat_id="oc_chat",
        topic="百度",
        updated_at=(
            datetime.now(timezone.utc) - timedelta(hours=9)
        ).isoformat(timespec="seconds"),
    )
    store.save(stale)

    assert session_is_stale(stale) is True
    assert store.load("oc_chat") is None


def test_a_corrupted_transcript_does_not_brick_the_bot(tmp_path):
    store = InterviewSessionStore(tmp_path / "interviews")
    store.directory.mkdir(parents=True, exist_ok=True)
    store.path_for("oc_chat").write_text("{not json", encoding="utf-8")

    assert store.load("oc_chat") is None


def test_summary_counts_rounds_and_average_without_the_model():
    session = InterviewSession(chat_id="oc_chat", topic="百度 Agent", planned_turns=3)
    session.ask("第 1 题：讲项目")
    session.answer("讲了 JD Radar")
    session.turns[0].score = 8.0
    session.ask("第 2 题：讲评测")
    session.answer("讲了 golden set")
    session.turns[1].score = 5.0

    summary = render_session_summary(session)

    assert "已完成：2/3 轮" in summary
    assert "平均分：6.5/10" in summary
    assert "第 1 题（8.0/10）" in summary


def test_coach_role_keeps_the_registry_role_id_so_grounding_still_loads():
    role = coach_role(make_registry())

    assert role.role_id == "interview_coach"
    assert role.display_name == "Interview Coach"
