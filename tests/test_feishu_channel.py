import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from lark_oapi.channel._coerce import coerce_outbound

import job_agent_harness.feishu_channel as feishu_channel
from job_agent_harness.feishu_channel import (
    FeishuBotBinding,
    LazyOrchestrator,
    binding_help,
    command_for_binding,
    configured_role_bot_ids,
    format_report_messages,
    split_markdown_message,
    unwrap_whole_body_fence,
    load_bot_bindings,
    register_message_handler,
    role_bot_env_names,
    send_checked,
    strip_bound_bot_mention,
)
from job_agent_harness.feishu_group import AgentTownGroup
from job_agent_harness.models import ModelReply, RoleSpec, RunMetrics, RunReport
from job_agent_harness.push_daily import build_delivery_plan
from job_agent_harness.voice import SpeechClip


def make_report(final_output: str) -> RunReport:
    return RunReport(
        run_id="12345678-abcd",
        query="test",
        mode="parallel",
        role_registry_version=1,
        results=[],
        final_output=final_output,
        metrics=RunMetrics(
            selected_roles=3,
            completed_roles=4,
            failed_roles=0,
            model_calls=4,
            wall_latency_ms=1234,
            sum_agent_latency_ms=2000,
            input_tokens=100,
            output_tokens=50,
            parallel_speedup_estimate=1.5,
        ),
    )


def test_long_answers_are_split_instead_of_truncated():
    """The old path cut at the budget and dropped the rest of a real JD list."""

    report = make_report("长" * 200)

    messages = format_report_messages(report, max_chars=120)

    assert len(messages) > 1
    assert all(len(message) <= 120 for message in messages)
    assert "".join(message.split("\n\n", 1)[1] for message in messages).count("长") == 200
    assert "run=12345678" in messages[-1]
    assert "roles=3" in messages[-1]
    assert "calls=4" in messages[-1]
    assert messages[0].startswith("（1/")


def test_a_short_answer_stays_one_message_without_a_counter():
    messages = format_report_messages(make_report("百度 J100679 仍然开放。"))

    assert len(messages) == 1
    assert messages[0].startswith("百度 J100679")


def test_splitting_never_leaves_a_code_fence_open():
    """This is what swallowed a whole Feishu reply.

    Cutting the body at a character budget removed the closing fence, so the
    remaining answer and the run footer rendered as one grey code box.
    """

    body = "\n".join(
        ["## 岗位 JD 原文", "```text", *[f"第 {index} 行 JD 正文" for index in range(40)], "```", "## 结论", "值得投。"]
    )

    messages = split_markdown_message(body, max_chars=200)

    assert len(messages) > 1
    for message in messages:
        assert message.count("```") % 2 == 0, message
    # No JD line is lost, and none of them leaks out of a code block.
    rejoined = "\n".join(messages)
    for index in range(40):
        assert f"第 {index} 行 JD 正文" in rejoined
    assert "值得投。" in rejoined


def test_a_reopened_block_keeps_its_language():
    body = "```json\n" + "\n".join(f'  "k{index}": {index},' for index in range(40)) + "\n```"

    messages = split_markdown_message(body, max_chars=200)

    assert len(messages) > 1
    assert messages[1].startswith("```json")


def test_an_answer_wrapped_entirely_in_a_fence_is_unwrapped():
    """Models sometimes wrap the whole reply in ```markdown.

    Feishu then shows monospace with no headings and no clickable JD link.
    """

    body = "```markdown\n### 行动清单\n- 投拼多多\n```"

    assert unwrap_whole_body_fence(body) == "### 行动清单\n- 投拼多多"


def test_a_genuine_code_block_is_not_unwrapped():
    body = "```bash\nuv run job-agent-watch\n```"

    assert unwrap_whole_body_fence(body) == body


def test_a_single_unsplittable_line_is_still_sent():
    messages = split_markdown_message("长" * 500, max_chars=100)

    assert len(messages) == 5
    assert all(len(message) <= 100 for message in messages)


def test_send_checked_raises_when_sdk_returns_failure():
    class FakeChannel:
        async def send(self, to, message):
            return SimpleNamespace(success=False, error="bad request")

    with pytest.raises(RuntimeError, match="bad request"):
        asyncio.run(send_checked(FakeChannel(), "chat", {"text": "hello"}))


def test_send_checked_returns_successful_result():
    expected = SimpleNamespace(success=True, error=None)

    class FakeChannel:
        async def send(self, to, message):
            return expected

    result = asyncio.run(
        send_checked(FakeChannel(), "chat", {"markdown": "# hello"})
    )

    assert result is expected


@pytest.mark.asyncio
async def test_lazy_orchestrator_builds_once_under_concurrency():
    created = []

    class FakeOrchestrator:
        async def run(self, request):
            return f"done:{request}"

    def factory():
        created.append(True)
        return FakeOrchestrator()

    orchestrator = LazyOrchestrator(factory)
    results = await asyncio.gather(
        orchestrator.run("a"),
        orchestrator.run("b"),
    )

    assert results == ["done:a", "done:b"]
    assert created == [True]


def make_role(role_id: str, display_name: str, enabled: bool = True):
    return RoleSpec(
        role_id=role_id,
        display_name=display_name,
        goal="为求职任务提供可以核验的专业建议",
        system_prompt="只根据用户提供的信息回答，并明确标记所有待核验内容。",
        enabled=enabled,
    )


def test_load_bot_bindings_supports_controller_and_named_role_bots():
    roles = {
        "job_scout": make_role("job_scout", "Job Scout"),
        "jd_analyst": make_role("jd_analyst", "JD Analyst"),
    }
    registry = SimpleNamespace(get=roles.__getitem__)
    env = {
        "LARK_APP_ID": "cli_controller",
        "LARK_APP_SECRET": "controller-secret",
        "JOB_AGENT_FEISHU_ROLE_BOTS": "job_scout,jd_analyst",
        "LARK_ROLE_JOB_SCOUT_APP_ID": "cli_scout",
        "LARK_ROLE_JOB_SCOUT_APP_SECRET": "scout-secret",
        "LARK_ROLE_JD_ANALYST_APP_ID": "cli_jd",
        "LARK_ROLE_JD_ANALYST_APP_SECRET": "jd-secret",
    }

    bindings = load_bot_bindings(registry, env)

    assert [binding.identity_label for binding in bindings] == [
        "controller",
        "job_scout",
        "jd_analyst",
    ]
    assert bindings[1].display_name == "Job Scout"


def test_load_bot_bindings_can_select_one_process_isolated_identity():
    roles = {
        "job_scout": make_role("job_scout", "Job Scout"),
        "jd_analyst": make_role("jd_analyst", "JD Analyst"),
    }
    registry = SimpleNamespace(get=roles.__getitem__)
    env = {
        "LARK_APP_ID": "cli_controller",
        "LARK_APP_SECRET": "controller-secret",
        "JOB_AGENT_FEISHU_ROLE_BOTS": "job_scout,jd_analyst",
        "JOB_AGENT_FEISHU_BINDING": "jd_analyst",
        "LARK_ROLE_JOB_SCOUT_APP_ID": "cli_scout",
        "LARK_ROLE_JOB_SCOUT_APP_SECRET": "scout-secret",
        "LARK_ROLE_JD_ANALYST_APP_ID": "cli_jd",
        "LARK_ROLE_JD_ANALYST_APP_SECRET": "jd-secret",
    }

    bindings = load_bot_bindings(registry, env)

    assert [binding.identity_label for binding in bindings] == [
        "jd_analyst"
    ]


def test_load_bot_bindings_rejects_unknown_selected_identity():
    registry = SimpleNamespace(
        get=lambda role_id: make_role(role_id, "Job Scout")
    )
    env = {
        "LARK_APP_ID": "cli_controller",
        "LARK_APP_SECRET": "controller-secret",
        "JOB_AGENT_FEISHU_BINDING": "missing_role",
    }

    with pytest.raises(ValueError, match="missing_role"):
        load_bot_bindings(registry, env)


def test_load_bot_bindings_rejects_missing_role_credentials():
    registry = SimpleNamespace(
        get=lambda role_id: make_role(role_id, "Job Scout")
    )
    env = {
        "JOB_AGENT_FEISHU_ROLE_BOTS": "job_scout",
        "LARK_ROLE_JOB_SCOUT_APP_ID": "cli_scout",
    }

    with pytest.raises(ValueError, match="APP_SECRET"):
        load_bot_bindings(registry, env)


def test_role_bot_env_names_are_stable():
    assert role_bot_env_names("job_knowledge_curator") == (
        "LARK_ROLE_JOB_KNOWLEDGE_CURATOR_APP_ID",
        "LARK_ROLE_JOB_KNOWLEDGE_CURATOR_APP_SECRET",
    )


def test_configured_role_bot_ids_are_deduplicated_in_order():
    assert configured_role_bot_ids(
        {
            "JOB_AGENT_FEISHU_ROLE_BOTS": (
                "job_scout,jd_analyst,job_scout"
            )
        }
    ) == ["job_scout", "jd_analyst"]


def test_bound_bot_routes_plain_question_to_its_role():
    binding = FeishuBotBinding(
        app_id="cli_scout",
        app_secret="secret",
        display_name="Job Scout",
        role_id="job_scout",
    )

    command = command_for_binding("核验这个岗位", binding)

    assert command.requested_roles == ["job_scout"]
    assert command.mode == "single"
    assert command.label == "@Job Scout"


def test_strip_bound_bot_mention_only_removes_bot_prefix():
    binding = FeishuBotBinding(
        app_id="cli_scout",
        app_secret="secret",
        display_name="Job Scout",
        role_id="job_scout",
    )
    mentions = [
        SimpleNamespace(
            is_bot=True,
            key="@_user_1",
            name="Job Scout",
        ),
        SimpleNamespace(
            is_bot=False,
            key="@_user_2",
            name="Fairy",
        ),
    ]

    assert (
        strip_bound_bot_mention(
            "@Job Scout：帮 @Fairy 核验岗位",
            binding,
            mentions,
        )
        == "帮 @Fairy 核验岗位"
    )


def test_judge_binding_help_explains_single_audit():
    binding = FeishuBotBinding(
        app_id="cli_judge",
        app_secret="secret",
        display_name="Evidence Judge",
        role_id="judge",
    )

    assert "不再重复调用 Judge" in binding_help(binding)


@pytest.mark.asyncio
async def test_controller_group_create_routes_sender_and_all_role_apps(
    monkeypatch,
    tmp_path,
):
    class FakeChannel:
        def __init__(self):
            self.callback = None
            self.sent = []

        def on(self, event, callback):
            assert event == "message"
            self.callback = callback

        async def send(self, to, message):
            self.sent.append((to, message))
            return SimpleNamespace(success=True, error=None)

    captured = {}

    async def fake_ensure(**kwargs):
        captured.update(kwargs)
        return AgentTownGroup(chat_id="oc_town", created=True)

    monkeypatch.setattr(feishu_channel, "runtime_data_dir", lambda: tmp_path)
    monkeypatch.setattr(
        feishu_channel,
        "configured_role_bot_ids",
        lambda: [f"role_{index}" for index in range(7)],
    )
    monkeypatch.setattr(
        feishu_channel,
        "configured_role_app_ids",
        lambda *_args: [f"cli_role_{index}" for index in range(7)],
    )
    monkeypatch.setattr(
        feishu_channel,
        "ensure_agent_town_group",
        fake_ensure,
    )
    channel = FakeChannel()
    binding = FeishuBotBinding(
        app_id="cli_controller",
        app_secret="controller-secret",
    )
    register_message_handler(
        channel,
        binding,
        SimpleNamespace(),
        SimpleNamespace(),
        set(),
    )

    await channel.callback(
        SimpleNamespace(
            content_text="/group-create",
            mentions=(),
            chat_id="oc_private",
            sender_id="ou_owner",
        )
    )

    assert captured["owner_id"] == "ou_owner"
    assert captured["owner_id_type"] == "open_id"
    assert captured["role_app_ids"] == [
        f"cli_role_{index}" for index in range(7)
    ]
    assert [target for target, _ in channel.sent] == [
        "oc_private",
        "oc_town",
    ]


@pytest.mark.asyncio
async def test_role_bot_rejects_group_create_command(tmp_path, monkeypatch):
    class FakeChannel:
        def on(self, _event, callback):
            self.callback = callback

        async def send(self, to, message):
            self.sent = (to, message)
            return SimpleNamespace(success=True, error=None)

    monkeypatch.setattr(feishu_channel, "runtime_data_dir", lambda: tmp_path)
    channel = FakeChannel()
    binding = FeishuBotBinding(
        app_id="cli_scout",
        app_secret="secret",
        display_name="Job Scout",
        role_id="job_scout",
    )
    register_message_handler(
        channel,
        binding,
        SimpleNamespace(),
        SimpleNamespace(),
        {"job_scout"},
    )

    await channel.callback(
        SimpleNamespace(
            content_text="/group-create",
            mentions=(),
            chat_id="oc_chat",
            sender_id="ou_owner",
        )
    )

    assert "总控机器人" in channel.sent[1]["text"]


def test_daily_delivery_prefers_independent_role_bots():
    controller = FeishuBotBinding(
        app_id="cli_controller",
        app_secret="secret",
        display_name="总控",
    )
    scout = FeishuBotBinding(
        app_id="cli_scout",
        app_secret="secret",
        display_name="Job Scout",
        role_id="job_scout",
    )
    judge = FeishuBotBinding(
        app_id="cli_judge",
        app_secret="secret",
        display_name="Evidence Judge",
        role_id="judge",
    )

    plan = build_delivery_plan(
        [controller, scout, judge],
        role_messages={
            "job_scout": ["岗位消息"],
            "judge": ["审核消息"],
        },
        fallback_messages=["综合日报"],
    )

    assert [(binding.identity_label, messages) for binding, messages in plan] == [
        ("job_scout", ["岗位消息"]),
        ("judge", ["审核消息"]),
    ]


def test_daily_delivery_can_target_one_role_or_fall_back_to_controller():
    controller = FeishuBotBinding(
        app_id="cli_controller",
        app_secret="secret",
        display_name="总控",
    )
    scout = FeishuBotBinding(
        app_id="cli_scout",
        app_secret="secret",
        display_name="Job Scout",
        role_id="job_scout",
    )

    targeted = build_delivery_plan(
        [controller, scout],
        role_messages={"job_scout": ["岗位消息"]},
        fallback_messages=["综合日报"],
        requested_role="job_scout",
    )
    fallback = build_delivery_plan(
        [controller],
        role_messages={},
        fallback_messages=["综合日报"],
    )

    assert targeted == [(scout, ["岗位消息"])]
    assert fallback == [(controller, ["综合日报"])]


class InterviewChannel:
    """A Feishu channel that records what it sent and can serve audio."""

    def __init__(self, audio: bytes = b"opus-bytes"):
        self.callback = None
        self.sent = []
        self.audio = audio
        self.downloads = []

    def on(self, event, callback):
        assert event == "message"
        self.callback = callback

    async def send(self, to, message):
        # Run the payload through the SDK's own coercion, or a wrong key (the
        # real `{"audio": {"path": ...}}` bug) passes every test and fails only
        # against live Feishu.
        coerce_outbound(message)
        self.sent.append((to, message))
        return SimpleNamespace(success=True, error=None)

    async def download_resource(self, file_key, resource_type, message_id=None):
        self.downloads.append((file_key, resource_type, message_id))
        return self.audio


COACH_ROLE = RoleSpec(
    role_id="interview_coach",
    display_name="Interview Coach",
    goal="围绕目标 JD 生成问题、追问、评分标准和学习任务",
    system_prompt="你是 Interview Coach。固定输出：知识题、项目深挖题、评分 rubric。",
)


def coach_binding() -> FeishuBotBinding:
    return FeishuBotBinding(
        app_id="cli_coach",
        app_secret="secret",
        display_name="Interview Coach",
        role_id="interview_coach",
    )


class ScriptedOrchestrator:
    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []
        self.runs = []

    async def complete(self, role, query):
        self.prompts.append(query)
        return ModelReply(
            content=self.replies.pop(0) if self.replies else "评分：6/10\n点评：还行"
        )

    async def run(self, request):
        self.runs.append(request)
        return make_report("常规单角色回答")


def register_coach(channel, orchestrator):
    register_message_handler(
        channel,
        coach_binding(),
        SimpleNamespace(get=lambda _role_id: COACH_ROLE),
        orchestrator,
        {"interview_coach"},
    )


def text_message(text: str, chat_id: str = "oc_chat"):
    return SimpleNamespace(
        content_text=text,
        mentions=(),
        chat_id=chat_id,
        sender_id="ou_candidate",
        message_id="om_text",
        resources=[],
        content=SimpleNamespace(kind="text"),
    )


def voice_message(chat_id: str = "oc_chat"):
    return SimpleNamespace(
        content_text="",
        mentions=(),
        chat_id=chat_id,
        sender_id="ou_candidate",
        message_id="om_voice",
        resources=[SimpleNamespace(type="audio", file_key="file_voice_1")],
        content=SimpleNamespace(kind="audio", file_key="file_voice_1"),
    )


@pytest.mark.asyncio
async def test_a_spoken_answer_is_transcribed_and_scored(monkeypatch, tmp_path):
    """A voice message has no ``content_text``; without STT the turn is lost."""

    monkeypatch.setattr(feishu_channel, "runtime_data_dir", lambda: tmp_path)

    async def fake_transcribe(audio, *, app_id, app_secret, post=None):
        assert audio == b"opus-bytes"
        assert app_id == "cli_coach"
        return "我用 30 条 golden set 做的评测"

    monkeypatch.setattr(feishu_channel, "transcribe_opus", fake_transcribe)
    channel = InterviewChannel()
    orchestrator = ScriptedOrchestrator(
        [
            "第 1 题：讲一个你做过的多 Agent 项目。",
            "评分：7/10\n点评：证据具体。\n下一题：延迟怎么测？",
        ]
    )
    register_coach(channel, orchestrator)

    await channel.callback(text_message("/mock start 百度 Agent 应用全栈"))
    await channel.callback(voice_message())

    assert channel.downloads == [("file_voice_1", "audio", "om_voice")]
    replies = [message for _to, message in channel.sent]
    # The candidate sees what was heard, so a mis-transcription is visible.
    assert any("我用 30 条 golden set" in message.get("text", "") for message in replies)
    assert any("下一题" in message.get("markdown", "") for message in replies)
    assert "语音转写" in orchestrator.prompts[1]
    assert orchestrator.runs == []


@pytest.mark.asyncio
async def test_a_failed_transcription_says_so_instead_of_answering_silence(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(feishu_channel, "runtime_data_dir", lambda: tmp_path)

    async def fail(*_args, **_kwargs):
        raise RuntimeError("no permission")

    monkeypatch.setattr(feishu_channel, "transcribe_opus", fail)
    channel = InterviewChannel()
    register_coach(channel, ScriptedOrchestrator([]))

    await channel.callback(voice_message())

    assert "no permission" in channel.sent[0][1]["text"]
    assert "speech_to_text:speech" in channel.sent[0][1]["text"]


@pytest.mark.asyncio
async def test_a_voice_session_gets_the_question_as_an_audio_message(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(feishu_channel, "runtime_data_dir", lambda: tmp_path)
    spoken = []

    async def fake_synthesis(text, directory, voice=None):
        clip = Path(directory)
        clip.mkdir(parents=True, exist_ok=True)
        path = clip / "coach.opus"
        path.write_bytes(b"opus")
        spoken.append(text)
        return SpeechClip(path=path)

    monkeypatch.setattr(
        feishu_channel,
        "synthesize_speech_async",
        fake_synthesis,
    )
    channel = InterviewChannel()
    register_coach(
        channel,
        ScriptedOrchestrator(["第 1 题：讲一个你做过的多 Agent 项目。"]),
    )

    await channel.callback(text_message("/mock start 百度 Agent 语音"))

    audio_sends = [
        message for _to, message in channel.sent if "audio" in message
    ]
    assert len(audio_sends) == 1
    assert spoken == ["第 1 题：讲一个你做过的多 Agent 项目。"]
    # The clip is a temp artefact, not something to accumulate in the data dir.
    assert not (tmp_path / "voice" / "coach.opus").exists()


@pytest.mark.asyncio
async def test_no_encoder_falls_back_to_text_with_one_hint(monkeypatch, tmp_path):
    monkeypatch.setattr(feishu_channel, "runtime_data_dir", lambda: tmp_path)

    async def no_clip(_text, _directory, voice=None):
        return None

    monkeypatch.setattr(feishu_channel, "synthesize_speech_async", no_clip)
    channel = InterviewChannel()
    register_coach(
        channel,
        ScriptedOrchestrator(
            [
                "第 1 题：讲一个项目。",
                "评分：7/10\n点评：不错。\n下一题：评测怎么做？",
            ]
        ),
    )

    await channel.callback(text_message("/mock start 百度 语音"))
    await channel.callback(text_message("我做了 JD Radar。"))

    hints = [
        message
        for _to, message in channel.sent
        if "brew install ffmpeg" in message.get("text", "")
    ]
    assert len(hints) == 1
    assert any("第 1 题" in message.get("markdown", "") for _to, message in channel.sent)


@pytest.mark.asyncio
async def test_a_rejected_audio_upload_names_the_missing_scope(monkeypatch, tmp_path):
    """Feishu needs `im:resource:upload` to accept a clip; text must survive."""

    monkeypatch.setattr(feishu_channel, "runtime_data_dir", lambda: tmp_path)

    async def fake_synthesis(text, directory, voice=None):
        clip = Path(directory)
        clip.mkdir(parents=True, exist_ok=True)
        path = clip / "coach.opus"
        path.write_bytes(b"opus")
        return SpeechClip(path=path)

    monkeypatch.setattr(feishu_channel, "synthesize_speech_async", fake_synthesis)

    class RejectingChannel(InterviewChannel):
        async def send(self, to, message):
            if "audio" in message:
                raise RuntimeError(
                    "Feishu send failed: 99991672 im:resource:upload required"
                )
            return await super().send(to, message)

    channel = RejectingChannel()
    register_coach(
        channel,
        ScriptedOrchestrator(
            [
                "第 1 题：讲一个项目。",
                "评分：7/10\n点评：不错。\n下一题：评测怎么做？",
            ]
        ),
    )

    await channel.callback(text_message("/mock start 百度 语音"))
    await channel.callback(text_message("我做了 JD Radar。"))

    hints = [
        message
        for _to, message in channel.sent
        if "im:resource:upload" in message.get("text", "")
    ]
    # Named once, not after every question, and it does not mention brew.
    assert len(hints) == 1
    assert "brew install" not in hints[0]["text"]
    # Both questions still arrived as text.
    assert sum("markdown" in message for _to, message in channel.sent) >= 2
    assert not (tmp_path / "voice" / "coach.opus").exists()


@pytest.mark.asyncio
async def test_a_plain_question_to_the_coach_bot_still_runs_the_pipeline(
    monkeypatch,
    tmp_path,
):
    """The coach owns ``/mock`` and open sessions only — nothing else."""

    monkeypatch.setattr(feishu_channel, "runtime_data_dir", lambda: tmp_path)
    channel = InterviewChannel()
    orchestrator = ScriptedOrchestrator([])
    register_coach(channel, orchestrator)

    await channel.callback(text_message("帮我准备百度 J99974 的面试"))

    assert [request.requested_roles for request in orchestrator.runs] == [
        ["interview_coach"]
    ]
    assert orchestrator.prompts == []
    assert any(
        "常规单角色回答" in message.get("markdown", "")
        for _to, message in channel.sent
    )


@pytest.mark.asyncio
async def test_the_coach_help_lists_the_mock_interview_commands():
    assert "/mock start" in binding_help(coach_binding())
    assert "语音" in binding_help(coach_binding())
