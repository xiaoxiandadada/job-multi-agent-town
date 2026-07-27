from __future__ import annotations

import argparse
import asyncio
import logging
import os

from lark_oapi.channel import FeishuChannel

from .daily_brief import (
    current_date,
    load_chat_id,
    load_daily_messages,
    load_role_daily_messages,
)
from .feishu_channel import FeishuBotBinding, load_bot_bindings
from .runtime import build_registry, prepare_directory, runtime_data_dir


def build_delivery_plan(
    bindings: list[FeishuBotBinding],
    *,
    role_messages: dict[str, list[str]],
    fallback_messages: list[str],
    requested_role: str | None = None,
    include_controller: bool = False,
) -> list[tuple[FeishuBotBinding, list[str]]]:
    controller = next(
        (binding for binding in bindings if binding.role_id is None),
        None,
    )
    role_bindings = [
        binding for binding in bindings if binding.role_id is not None
    ]
    if requested_role:
        role_bindings = [
            binding
            for binding in role_bindings
            if binding.role_id == requested_role
        ]
        if not role_bindings:
            raise ValueError(
                f"角色 {requested_role} 尚未绑定并启用独立飞书机器人"
            )

    plan = [
        (binding, role_messages[binding.role_id])
        for binding in role_bindings
        if binding.role_id in role_messages
    ]
    if controller and (include_controller or not plan):
        plan.insert(0, (controller, fallback_messages))
    if not plan:
        raise ValueError("没有可用于日报推送的飞书机器人身份")
    return plan


async def push_daily(
    target_date: str | None,
    full: bool,
    requested_role: str | None = None,
    include_controller: bool = False,
) -> None:
    chat_id = os.getenv("JOB_AGENT_FEISHU_CHAT_ID") or load_chat_id(
        runtime_data_dir()
    )
    resolved_date = target_date or current_date().isoformat()
    max_chars = int(os.getenv("JOB_AGENT_FEISHU_MAX_CHARS", "8000"))
    fallback_messages = load_daily_messages(
        prepare_directory(),
        resolved_date,
        mode="full" if full else "summary",
        max_chars=max_chars,
    )
    role_messages = load_role_daily_messages(
        prepare_directory(),
        resolved_date,
        max_chars=max_chars,
    )
    bindings = load_bot_bindings(build_registry())
    plan = build_delivery_plan(
        bindings,
        role_messages=role_messages,
        fallback_messages=fallback_messages,
        requested_role=requested_role,
        include_controller=include_controller or full,
    )
    sdk_logger = logging.getLogger("Lark")
    sdk_logger.setLevel(logging.WARNING)
    sdk_logger.propagate = False
    for handler in sdk_logger.handlers:
        handler.setLevel(logging.WARNING)

    pushed_messages = 0
    for binding, messages in plan:
        channel = FeishuChannel(
            app_id=binding.app_id,
            app_secret=binding.app_secret,
        )
        for markdown in messages:
            result = await channel.send(chat_id, {"markdown": markdown})
            if not result.success:
                raise RuntimeError(
                    f"{binding.display_name} 飞书推送失败：{result.error}"
                )
            pushed_messages += 1
    identities = ",".join(binding.identity_label for binding, _ in plan)
    print(
        f"pushed_daily_messages={pushed_messages} identities={identities}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Push a local AI job-search daily brief to the last Feishu chat."
    )
    parser.add_argument("--date", dest="target_date")
    parser.add_argument("--full", action="store_true")
    parser.add_argument(
        "--role",
        dest="requested_role",
        help="只用指定角色机器人推送它负责的日报部分",
    )
    parser.add_argument(
        "--controller",
        action="store_true",
        help="在角色分工推送之外，再由总控发送一份综合摘要",
    )
    args = parser.parse_args()
    asyncio.run(
        push_daily(
            args.target_date,
            args.full,
            args.requested_role,
            args.controller,
        )
    )


if __name__ == "__main__":
    main()
