from __future__ import annotations

import asyncio
import json
import os

from lark_oapi.channel import FeishuChannel

from .models import RoleSpec, RunRequest
from .runtime import build_orchestrator, build_registry


async def run_channel() -> None:
    app_id = os.environ["LARK_APP_ID"]
    app_secret = os.environ["LARK_APP_SECRET"]
    channel = FeishuChannel(app_id=app_id, app_secret=app_secret)
    registry = build_registry()
    orchestrator = build_orchestrator()

    async def on_message(message):
        text = message.content_text.strip()
        if text == "/roles":
            role_lines = [
                f"- {role.role_id}: {role.display_name}"
                for role in registry.list_roles()
            ]
            await channel.send(message.chat_id, {"text": "\n".join(role_lines)})
            return

        if text.startswith("/role-add "):
            try:
                role = RoleSpec.model_validate(
                    json.loads(text.removeprefix("/role-add ").strip())
                )
                version = registry.add(role)
                response = f"角色已添加，registry version={version}"
            except Exception as exc:
                response = f"添加失败：{exc}"
            await channel.send(message.chat_id, {"text": response})
            return

        requested_roles = []
        if text.startswith("/agent "):
            _, role_id, text = text.split(maxsplit=2)
            requested_roles = [role_id]
        report = await orchestrator.run(
            RunRequest(query=text, requested_roles=requested_roles)
        )
        await channel.send(
            message.chat_id,
            {
                "text": (
                    f"{report.final_output}\n\n"
                    f"run={report.run_id[:8]} · "
                    f"latency={report.metrics.wall_latency_ms:.0f}ms · "
                    f"calls={report.metrics.model_calls}"
                )
            },
        )

    channel.on("message", on_message)
    await channel.connect()


def main() -> None:
    asyncio.run(run_channel())
