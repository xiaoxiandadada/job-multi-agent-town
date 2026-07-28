from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass, field

from lark_oapi.channel import FeishuChannel

from .commands import HELP_TEXT, RunCommand, parse_run_command
from .daily_brief import (
    load_daily_messages,
    load_role_daily_messages,
    parse_daily_command,
)
from .feishu_group import (
    AGENT_TOWN_NAME,
    configured_role_app_ids,
    ensure_agent_town_group,
    remember_preferred_chat_id,
)
from .models import RoleSpec, RunReport, RunRequest
from .runtime import (
    build_orchestrator,
    build_registry,
    prepare_directory,
    runtime_data_dir,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FeishuBotBinding:
    """One Feishu application identity bound to zero or one expert role."""

    app_id: str
    app_secret: str = field(repr=False)
    display_name: str = "AI 求职 Multi-Agent"
    role_id: str | None = None

    @property
    def identity_label(self) -> str:
        return self.role_id or "controller"


class LazyOrchestrator:
    """Delay the heavy LangGraph/model stack until the first real task."""

    def __init__(self, factory):
        self._factory = factory
        self._orchestrator = None
        self._lock = asyncio.Lock()

    async def run(self, request):
        if self._orchestrator is None:
            async with self._lock:
                if self._orchestrator is None:
                    self._orchestrator = self._factory()
        return await self._orchestrator.run(request)


def role_bot_env_names(role_id: str) -> tuple[str, str]:
    prefix = re.sub(r"[^A-Z0-9]+", "_", role_id.upper()).strip("_")
    return (
        f"LARK_ROLE_{prefix}_APP_ID",
        f"LARK_ROLE_{prefix}_APP_SECRET",
    )


def configured_role_bot_ids(
    environ: Mapping[str, str] | None = None,
) -> list[str]:
    env = environ if environ is not None else os.environ
    return list(
        dict.fromkeys(
            role_id.strip()
            for role_id in env.get(
                "JOB_AGENT_FEISHU_ROLE_BOTS",
                "",
            ).split(",")
            if role_id.strip()
        )
    )


def load_bot_bindings(
    registry,
    environ: Mapping[str, str] | None = None,
) -> list[FeishuBotBinding]:
    env = environ if environ is not None else os.environ
    bindings: list[FeishuBotBinding] = []
    controller_id = env.get("LARK_APP_ID", "").strip()
    controller_secret = env.get("LARK_APP_SECRET", "").strip()
    if bool(controller_id) != bool(controller_secret):
        raise ValueError(
            "LARK_APP_ID 与 LARK_APP_SECRET 必须同时配置"
        )
    if controller_id:
        bindings.append(
            FeishuBotBinding(
                app_id=controller_id,
                app_secret=controller_secret,
            )
        )

    configured_role_ids = configured_role_bot_ids(env)
    for role_id in configured_role_ids:
        try:
            role = registry.get(role_id)
        except KeyError as exc:
            raise ValueError(
                f"未找到要绑定的飞书角色：{role_id}"
            ) from exc
        if not role.enabled:
            raise ValueError(f"飞书角色已禁用：{role_id}")
        app_id_env, app_secret_env = role_bot_env_names(role_id)
        app_id = env.get(app_id_env, "").strip()
        app_secret = env.get(app_secret_env, "").strip()
        if not app_id or not app_secret:
            raise ValueError(
                f"角色 {role_id} 缺少 {app_id_env} 或 {app_secret_env}"
            )
        bindings.append(
            FeishuBotBinding(
                app_id=app_id,
                app_secret=app_secret,
                display_name=role.display_name,
                role_id=role_id,
            )
        )

    if not bindings:
        raise ValueError(
            "至少配置总控机器人或一个角色机器人"
        )
    app_ids = [binding.app_id for binding in bindings]
    if len(app_ids) != len(set(app_ids)):
        raise ValueError("同一个飞书 App ID 不能绑定多个角色")

    selected_identity = env.get(
        "JOB_AGENT_FEISHU_BINDING",
        "",
    ).strip()
    if selected_identity:
        bindings = [
            binding
            for binding in bindings
            if binding.identity_label == selected_identity
        ]
        if not bindings:
            raise ValueError(
                "未找到飞书身份："
                f"{selected_identity}"
            )
    return bindings


def strip_bound_bot_mention(
    text: str,
    binding: FeishuBotBinding,
    mentions=(),
) -> str:
    """Remove the leading @bot marker while retaining mentions of other users."""

    value = text.strip()
    candidates = {f"@{binding.display_name}"}
    for mention in mentions or ():
        if getattr(mention, "is_bot", False):
            key = getattr(mention, "key", None)
            name = getattr(mention, "name", None)
            if key:
                candidates.add(key)
            if name:
                candidates.add(f"@{name}")
    for candidate in sorted(candidates, key=len, reverse=True):
        value = re.sub(
            rf"^{re.escape(candidate)}[\s,:：，]*",
            "",
            value,
            count=1,
        )
    return value.strip()


def command_for_binding(
    text: str,
    binding: FeishuBotBinding,
) -> RunCommand:
    if binding.role_id is None:
        return parse_run_command(text)
    if not text.strip():
        raise ValueError(
            f"请在群里 @{binding.display_name} 后输入问题。"
        )
    return RunCommand(
        query=text.strip(),
        requested_roles=[binding.role_id],
        mode="single",
        label=f"@{binding.display_name}",
    )


def binding_help(binding: FeishuBotBinding) -> str:
    if binding.role_id is None:
        return HELP_TEXT
    judge_note = (
        "回答后会自动交给证据审核员复核。"
        if binding.role_id != "judge"
        else "该身份只做证据审查，不再重复调用 Judge。"
    )
    return (
        f"# {binding.display_name}\n\n"
        f"- 绑定角色：`{binding.role_id}`\n"
        f"- 单聊：直接输入问题\n"
        f"- 群聊：`@{binding.display_name} <问题>`\n"
        f"- 审核：{judge_note}\n\n"
        "发送 `/roles` 查看完整团队；日报仍可使用 `/daily`。"
    )


async def send_checked(channel, to: str, message):
    result = await channel.send(to, message)
    if not result.success:
        raise RuntimeError(f"Feishu send failed: {result.error}")
    return result


def format_report_message(report: RunReport, max_chars: int = 8000) -> str:
    footer = (
        f"run={report.run_id[:8]} · "
        f"latency={report.metrics.wall_latency_ms:.0f}ms · "
        f"roles={report.metrics.selected_roles} · "
        f"calls={report.metrics.model_calls}"
    )
    body = report.final_output.strip() or "本次运行没有可用输出。"
    available = max(0, max_chars - len(footer) - 2)
    if len(body) > available:
        suffix = "\n\n[内容已截断]"
        body = body[: max(0, available - len(suffix))] + suffix
    return f"{body}\n\n{footer}"


def configure_sdk_logging() -> None:
    sdk_logger = logging.getLogger("Lark")
    sdk_logger.setLevel(logging.WARNING)
    sdk_logger.propagate = False
    for handler in sdk_logger.handlers:
        handler.setLevel(logging.WARNING)


def register_message_handler(
    channel,
    binding: FeishuBotBinding,
    registry,
    orchestrator,
    connected_role_ids: set[str],
) -> None:
    async def on_message(message):
        text = strip_bound_bot_mention(
            message.content_text,
            binding,
            getattr(message, "mentions", ()),
        )
        if binding.role_id is None:
            remember_preferred_chat_id(
                runtime_data_dir(),
                message.chat_id,
            )
        if text == "/group-create":
            if binding.role_id is not None:
                await send_checked(
                    channel,
                    message.chat_id,
                    {
                        "text": (
                            "请在与总控机器人 AI 求职 Multi-Agent "
                            "的私聊中发送 /group-create。"
                        )
                    },
                )
                return
            try:
                role_ids = configured_role_bot_ids()
                role_app_ids = configured_role_app_ids(
                    role_ids,
                    os.environ,
                    role_bot_env_names,
                )
                group = await ensure_agent_town_group(
                    controller_app_id=binding.app_id,
                    controller_app_secret=binding.app_secret,
                    owner_open_id=message.sender_id,
                    role_app_ids=role_app_ids,
                    data_dir=runtime_data_dir(),
                )
                action = "已创建" if group.created else "已绑定"
                confirmation = (
                    f"{action}「{AGENT_TOWN_NAME}」：总控 + "
                    f"{len(role_app_ids)} 个独立角色机器人已就位。"
                    "后续日报将优先推送到本群。"
                )
                await send_checked(
                    channel,
                    message.chat_id,
                    {"text": confirmation},
                )
                if group.chat_id != message.chat_id:
                    await send_checked(
                        channel,
                        group.chat_id,
                        {
                            "markdown": (
                                f"# {AGENT_TOWN_NAME}已启动\n\n"
                                f"- 总控：1 个\n"
                                f"- 独立角色机器人：{len(role_app_ids)} 个\n"
                                "- 编排：LangGraph\n"
                                "- 日报：各角色可按职责分别推送\n\n"
                                "发送 `/roles` 查看团队，或直接 "
                                "@对应角色机器人提问。"
                            )
                        },
                    )
            except Exception as exc:
                await send_checked(
                    channel,
                    message.chat_id,
                    {
                        "text": (
                            f"Agent 小镇群创建失败：{exc}\n"
                            "请确认总控已开通 im:chat:create 与 "
                            "im:chat.members:write_only。"
                        )
                    },
                )
            return
        if text == "/help":
            await send_checked(
                channel,
                message.chat_id,
                {"markdown": binding_help(binding)},
            )
            return

        if text == "/roles":
            role_lines = [
                (
                    f"- `@{role.display_name}` (`{role.role_id}`)"
                    if role.role_id in connected_role_ids
                    else f"- `{role.role_id}`：{role.display_name}"
                )
                for role in registry.list_roles()
            ]
            roles_markdown = (
                f"# 当前角色（{len(role_lines)} 个）\n\n"
                + "\n".join(role_lines)
                + "\n\n带 `@` 的角色已经拥有独立飞书机器人身份；"
                "其他角色仍可通过总控 `/ask <role_id> <问题>` 调用。"
            )
            await send_checked(
                channel,
                message.chat_id,
                {"markdown": roles_markdown},
            )
            return

        if text.startswith("/daily"):
            try:
                target_date, mode = parse_daily_command(text)
                max_chars = int(
                    os.getenv("JOB_AGENT_FEISHU_MAX_CHARS", "8000")
                )
                if binding.role_id is None or mode == "full":
                    messages = load_daily_messages(
                        prepare_directory(),
                        target_date,
                        mode=mode,
                        max_chars=max_chars,
                    )
                else:
                    messages = load_role_daily_messages(
                        prepare_directory(),
                        target_date,
                        max_chars=max_chars,
                    )[binding.role_id]
                for markdown in messages:
                    await send_checked(
                        channel,
                        message.chat_id,
                        {"markdown": markdown},
                    )
            except Exception as exc:
                await send_checked(
                    channel,
                    message.chat_id,
                    {"text": f"日报推送失败：{exc}"},
                )
            return

        if text.startswith("/role-add "):
            if binding.role_id is not None:
                await send_checked(
                    channel,
                    message.chat_id,
                    {
                        "text": (
                            "新增角色请发送给总控机器人 "
                            "AI 求职 Multi-Agent。"
                        )
                    },
                )
                return
            try:
                role = RoleSpec.model_validate(
                    json.loads(text.removeprefix("/role-add ").strip())
                )
                version = registry.add(role)
                response = f"角色已添加，registry version={version}"
            except Exception as exc:
                response = f"添加失败：{exc}"
            await send_checked(channel, message.chat_id, {"text": response})
            return

        try:
            command = command_for_binding(text, binding)
        except ValueError as exc:
            await send_checked(channel, message.chat_id, {"text": str(exc)})
            return

        if command.label:
            await send_checked(
                channel,
                message.chat_id,
                {
                    "text": (
                        f"已启动「{command.label}」："
                        f"{len(command.requested_roles)} 个工作角色，完成后由 Judge 汇总。"
                    )
                },
            )

        logger.info(
            "run started label=%s roles=%s mode=%s",
            command.label or "auto",
            len(command.requested_roles),
            command.mode,
        )
        try:
            report = await orchestrator.run(
                RunRequest(
                    query=command.query,
                    requested_roles=command.requested_roles,
                    mode=command.mode,
                    use_judge=binding.role_id != "judge",
                )
            )
        except Exception as exc:
            await send_checked(
                channel,
                message.chat_id,
                {"text": f"运行失败：{exc}"},
            )
            return

        logger.info(
            "run completed run=%s latency_ms=%.0f roles=%s calls=%s failed=%s",
            report.run_id[:8],
            report.metrics.wall_latency_ms,
            report.metrics.selected_roles,
            report.metrics.model_calls,
            report.metrics.failed_roles,
        )
        max_chars = int(os.getenv("JOB_AGENT_FEISHU_MAX_CHARS", "8000"))
        try:
            await send_checked(
                channel,
                message.chat_id,
                {"markdown": format_report_message(report, max_chars=max_chars)},
            )
        except Exception:
            logger.exception(
                "full Feishu result send failed for run=%s",
                report.run_id[:8],
            )
            await send_checked(
                channel,
                message.chat_id,
                {
                    "text": (
                        "结果已生成，但完整内容回包失败。"
                        "请缩小任务范围后重试。\n\n"
                        f"run={report.run_id[:8]} · "
                        f"roles={report.metrics.selected_roles} · "
                        f"calls={report.metrics.model_calls}"
                    )
                },
            )

    channel.on("message", on_message)


async def run_channel() -> None:
    configure_sdk_logging()
    registry = build_registry()
    orchestrator = LazyOrchestrator(
        lambda: build_orchestrator(registry=registry)
    )
    bindings = load_bot_bindings(registry)
    if len(bindings) != 1:
        raise RuntimeError(
            "一个 worker 只能运行一个飞书身份；"
            "请通过 main() 启动多身份 supervisor"
        )
    connected_role_ids = set(configured_role_bot_ids())
    channels = []
    try:
        for binding in bindings:
            channel = FeishuChannel(
                app_id=binding.app_id,
                app_secret=binding.app_secret,
            )
            register_message_handler(
                channel,
                binding,
                registry,
                orchestrator,
                connected_role_ids,
            )
            channels.append((binding, channel))

        binding, channel = channels[0]
        await channel.connect_until_ready(timeout=30)
        for binding, _ in channels:
            logger.info(
                "Feishu identity connected name=%s role=%s",
                binding.display_name,
                binding.identity_label,
            )
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for stop_signal in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(
                    stop_signal,
                    stop_event.set,
                )
            except NotImplementedError:
                pass
        await stop_event.wait()
    finally:
        await asyncio.gather(
            *(channel.disconnect() for _, channel in channels),
            return_exceptions=True,
        )


def run_supervisor(identity_labels: list[str]) -> None:
    """Run each Feishu WebSocket identity in an isolated process.

    The upstream SDK owns one module-level asyncio loop for its WebSocket
    client. Process isolation prevents multiple app identities from attempting
    to drive the same loop while keeping one operational entry point.
    """

    workers: list[tuple[str, subprocess.Popen]] = []
    stopping = False

    def stop_workers(*_args) -> None:
        nonlocal stopping
        if stopping:
            return
        stopping = True
        for _, worker in workers:
            if worker.poll() is None:
                worker.terminate()

    previous_handlers = {}
    for stop_signal in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[stop_signal] = signal.getsignal(stop_signal)
        signal.signal(stop_signal, stop_workers)

    try:
        start_stagger_seconds = max(
            0.0,
            float(
                os.getenv(
                    "JOB_AGENT_FEISHU_START_STAGGER_SECONDS",
                    "1.5",
                )
            ),
        )
        for index, identity_label in enumerate(identity_labels):
            child_env = os.environ.copy()
            child_env["JOB_AGENT_FEISHU_BINDING"] = identity_label
            worker = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    (
                        "from job_agent_harness.feishu_channel "
                        "import main; main()"
                    ),
                ],
                env=child_env,
            )
            workers.append((identity_label, worker))
            logger.info(
                "Feishu worker started role=%s pid=%s",
                identity_label,
                worker.pid,
            )
            if index < len(identity_labels) - 1 and start_stagger_seconds:
                time.sleep(start_stagger_seconds)

        while not stopping:
            for identity_label, worker in workers:
                return_code = worker.poll()
                if return_code is None:
                    continue
                stop_workers()
                raise RuntimeError(
                    "飞书身份 worker 异常退出："
                    f"{identity_label} code={return_code}"
                )
            time.sleep(0.5)
    finally:
        stop_workers()
        for _, worker in workers:
            if worker.poll() is None:
                try:
                    worker.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    worker.kill()
            else:
                worker.wait()
        for stop_signal, previous_handler in previous_handlers.items():
            signal.signal(stop_signal, previous_handler)


def main() -> None:
    logging.basicConfig(
        level=os.getenv("JOB_AGENT_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    registry = build_registry()
    bindings = load_bot_bindings(registry)
    selected_identity = os.getenv(
        "JOB_AGENT_FEISHU_BINDING",
        "",
    ).strip()
    if not selected_identity and len(bindings) > 1:
        run_supervisor(
            [binding.identity_label for binding in bindings]
        )
        return
    asyncio.run(run_channel())
