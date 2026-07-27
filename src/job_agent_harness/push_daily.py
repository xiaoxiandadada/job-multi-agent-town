from __future__ import annotations

import argparse
import asyncio
import logging
import os
import time
import uuid

from lark_oapi.channel import FeishuChannel

from .daily_brief import (
    current_date,
    load_chat_id,
    load_daily_messages,
    load_role_daily_messages,
)
from .feishu_channel import FeishuBotBinding, load_bot_bindings
from .runtime import (
    build_activity_store,
    build_memory_store,
    build_registry,
    prepare_directory,
    runtime_data_dir,
)


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
    started = time.perf_counter()
    run_id = f"daily-{uuid.uuid4()}"
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
    activity_store = build_activity_store()
    memory_store = build_memory_store()
    selected_role_ids = [
        binding.role_id
        for binding, _ in plan
        if binding.role_id is not None
    ]
    activity_store.record(
        run_id=run_id,
        kind="run_started",
        status="running",
        orchestrator="daily-push",
        phase="route",
        mode="daily",
        query=f"推送 {resolved_date} 求职日报",
    )
    activity_store.record(
        run_id=run_id,
        kind="route_completed",
        status="completed",
        orchestrator="daily-push",
        phase="route",
        mode="daily",
        selected_role_ids=selected_role_ids,
    )
    activity_store.record(
        run_id=run_id,
        kind="phase_started",
        status="running",
        orchestrator="daily-push",
        phase="action",
        mode="daily",
        selected_role_ids=selected_role_ids,
    )
    sdk_logger = logging.getLogger("Lark")
    sdk_logger.setLevel(logging.WARNING)
    sdk_logger.propagate = False
    for handler in sdk_logger.handlers:
        handler.setLevel(logging.WARNING)

    pushed_messages = 0
    active_binding: FeishuBotBinding | None = None
    try:
        for binding, messages in plan:
            active_binding = binding
            if binding.role_id is not None:
                activity_store.record(
                    run_id=run_id,
                    kind="daily_push_started",
                    status="running",
                    orchestrator="daily-push",
                    phase="action",
                    mode="daily",
                    role_id=binding.role_id,
                    display_name=binding.display_name,
                    query=f"推送 {resolved_date} 角色日报",
                )
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
            if binding.role_id is not None:
                summary = (
                    f"已推送 {resolved_date} 日报，共 {len(messages)} 条消息"
                )
                activity_store.record(
                    run_id=run_id,
                    kind="daily_push_completed",
                    status="ok",
                    orchestrator="daily-push",
                    phase="action",
                    mode="daily",
                    role_id=binding.role_id,
                    display_name=binding.display_name,
                    output=summary,
                )
                memory_store.append(
                    role_id=binding.role_id,
                    run_id=run_id,
                    kind="observation",
                    text=f"{summary}。内容摘要：{messages[0][:1200]}",
                )
            active_binding = None
    except Exception as exc:
        if active_binding is not None and active_binding.role_id is not None:
            activity_store.record(
                run_id=run_id,
                kind="daily_push_completed",
                status="error",
                orchestrator="daily-push",
                phase="action",
                mode="daily",
                role_id=active_binding.role_id,
                display_name=active_binding.display_name,
                error=str(exc),
            )
        activity_store.record(
            run_id=run_id,
            kind="run_failed",
            status="error",
            orchestrator="daily-push",
            phase="system",
            mode="daily",
            error=str(exc),
        )
        raise
    wall_ms = (time.perf_counter() - started) * 1000
    activity_store.record(
        run_id=run_id,
        kind="run_completed",
        status="completed",
        orchestrator="daily-push",
        phase="system",
        mode="daily",
        output=f"{resolved_date} 日报推送完成",
        metrics={
            "selected_roles": len(selected_role_ids),
            "completed_roles": len(selected_role_ids),
            "failed_roles": 0,
            "model_calls": 0,
            "wall_latency_ms": wall_ms,
            "sum_agent_latency_ms": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "parallel_speedup_estimate": 1,
        },
    )
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
