import asyncio
from types import SimpleNamespace

import pytest

import job_agent_harness.feishu_channel as feishu_channel
from job_agent_harness.feishu_channel import (
    FeishuBotBinding,
    LazyOrchestrator,
    binding_help,
    command_for_binding,
    configured_role_bot_ids,
    format_report_message,
    load_bot_bindings,
    register_message_handler,
    role_bot_env_names,
    send_checked,
    strip_bound_bot_mention,
)
from job_agent_harness.feishu_group import AgentTownGroup
from job_agent_harness.models import RoleSpec, RunMetrics, RunReport
from job_agent_harness.push_daily import build_delivery_plan


def test_format_report_message_truncates_body_and_keeps_metrics():
    report = RunReport(
        run_id="12345678-abcd",
        query="test",
        mode="parallel",
        role_registry_version=1,
        results=[],
        final_output="长" * 200,
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

    message = format_report_message(report, max_chars=120)

    assert len(message) <= 120
    assert "[内容已截断]" in message
    assert "run=12345678" in message
    assert "roles=3" in message
    assert "calls=4" in message


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
        "job_scout": make_role("job_scout", "岗位侦察员"),
        "jd_analyst": make_role("jd_analyst", "JD 分析师"),
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
    assert bindings[1].display_name == "岗位侦察员"


def test_load_bot_bindings_can_select_one_process_isolated_identity():
    roles = {
        "job_scout": make_role("job_scout", "岗位侦察员"),
        "jd_analyst": make_role("jd_analyst", "JD 分析师"),
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
        get=lambda role_id: make_role(role_id, "岗位侦察员")
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
        get=lambda role_id: make_role(role_id, "岗位侦察员")
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
        display_name="岗位侦察员",
        role_id="job_scout",
    )

    command = command_for_binding("核验这个岗位", binding)

    assert command.requested_roles == ["job_scout"]
    assert command.mode == "single"
    assert command.label == "@岗位侦察员"


def test_strip_bound_bot_mention_only_removes_bot_prefix():
    binding = FeishuBotBinding(
        app_id="cli_scout",
        app_secret="secret",
        display_name="岗位侦察员",
        role_id="job_scout",
    )
    mentions = [
        SimpleNamespace(
            is_bot=True,
            key="@_user_1",
            name="岗位侦察员",
        ),
        SimpleNamespace(
            is_bot=False,
            key="@_user_2",
            name="Fairy",
        ),
    ]

    assert (
        strip_bound_bot_mention(
            "@岗位侦察员：帮 @Fairy 核验岗位",
            binding,
            mentions,
        )
        == "帮 @Fairy 核验岗位"
    )


def test_judge_binding_help_explains_single_audit():
    binding = FeishuBotBinding(
        app_id="cli_judge",
        app_secret="secret",
        display_name="证据审核员",
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

    assert captured["owner_open_id"] == "ou_owner"
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
        display_name="岗位侦察员",
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
        display_name="岗位侦察员",
        role_id="job_scout",
    )
    judge = FeishuBotBinding(
        app_id="cli_judge",
        app_secret="secret",
        display_name="证据审核员",
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
        display_name="岗位侦察员",
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
