from __future__ import annotations

import json
import os
import tempfile
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx

from .daily_brief import remember_chat_id


FEISHU_API_BASE = "https://open.feishu.cn/open-apis"
AGENT_TOWN_NAME = "AI 求职 Agent 小镇"
GROUP_STATE_FILENAME = "feishu_group.json"
CREATE_BOT_LIMIT = 5
UserIdType = Literal["open_id", "union_id", "user_id"]


@dataclass(frozen=True)
class AgentTownGroup:
    chat_id: str
    name: str = AGENT_TOWN_NAME
    created: bool = False


class FeishuGroupError(RuntimeError):
    """A safe Feishu group provisioning error without credentials."""


def configured_role_app_ids(
    role_ids: Sequence[str],
    environ: Mapping[str, str],
    env_name_factory,
) -> list[str]:
    app_ids: list[str] = []
    for role_id in role_ids:
        app_id_env, _ = env_name_factory(role_id)
        app_id = environ.get(app_id_env, "").strip()
        if not app_id:
            raise ValueError(f"角色 {role_id} 缺少 {app_id_env}")
        app_ids.append(app_id)
    if len(app_ids) != len(set(app_ids)):
        raise ValueError("角色机器人的飞书 App ID 必须互不相同")
    return app_ids


def load_agent_town_group(data_dir: Path) -> AgentTownGroup | None:
    path = data_dir / GROUP_STATE_FILENAME
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    chat_id = str(payload.get("chat_id", "")).strip()
    if not chat_id:
        raise ValueError("Agent 小镇群状态缺少 chat_id")
    return AgentTownGroup(
        chat_id=chat_id,
        name=str(payload.get("name") or AGENT_TOWN_NAME),
        created=False,
    )


def remember_preferred_chat_id(data_dir: Path, incoming_chat_id: str) -> str:
    """Keep daily delivery pinned to the town once it has been provisioned."""

    existing = load_agent_town_group(data_dir)
    target_chat_id = existing.chat_id if existing else incoming_chat_id
    remember_chat_id(data_dir, target_chat_id)
    return target_chat_id


def _save_agent_town_group(data_dir: Path, group: AgentTownGroup) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(
            {"chat_id": group.chat_id, "name": group.name},
            ensure_ascii=False,
        )
        + "\n"
    )
    fd, temp_name = tempfile.mkstemp(
        dir=data_dir,
        prefix=".feishu-group.",
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(temp_name, data_dir / GROUP_STATE_FILENAME)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _response_payload(response: httpx.Response, action: str) -> dict[str, Any]:
    try:
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise FeishuGroupError(f"{action}请求失败") from exc
    if payload.get("code") != 0:
        code = payload.get("code", "unknown")
        message = str(payload.get("msg") or "未知错误")
        raise FeishuGroupError(f"{action}失败（{code}）：{message}")
    return payload


async def _provision_agent_town_group(
    *,
    client,
    controller_app_id: str,
    controller_app_secret: str,
    owner_id: str,
    owner_id_type: UserIdType,
    role_app_ids: Sequence[str],
) -> AgentTownGroup:
    token_response = await client.post(
        f"{FEISHU_API_BASE}/auth/v3/tenant_access_token/internal",
        json={
            "app_id": controller_app_id,
            "app_secret": controller_app_secret,
        },
    )
    token_payload = _response_payload(token_response, "获取应用凭证")
    token = str(token_payload.get("tenant_access_token", "")).strip()
    if not token:
        raise FeishuGroupError("获取应用凭证失败：响应缺少 token")

    headers = {"Authorization": f"Bearer {token}"}
    first_role_apps = list(role_app_ids[:CREATE_BOT_LIMIT])
    remaining_role_apps = list(role_app_ids[CREATE_BOT_LIMIT:])
    request_uuid = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            (
                f"feishu-agent-town:{controller_app_id}:"
                f"{owner_id_type}:{owner_id}"
            ),
        )
    )
    create_response = await client.post(
        f"{FEISHU_API_BASE}/im/v1/chats",
        params={
            "user_id_type": owner_id_type,
            "set_bot_manager": "true",
            "uuid": request_uuid,
        },
        headers=headers,
        json={
            "name": AGENT_TOWN_NAME,
            "description": (
                "由总控与 7 个独立角色机器人协作的 AI 求职 Agent 小镇"
            ),
            "chat_mode": "group",
            "chat_type": "private",
            "group_message_type": "chat",
            "owner_id": owner_id,
            "user_id_list": [owner_id],
            "bot_id_list": first_role_apps,
        },
    )
    create_payload = _response_payload(create_response, "创建 Agent 小镇群")
    chat_id = str(
        (create_payload.get("data") or {}).get("chat_id", "")
    ).strip()
    if not chat_id:
        raise FeishuGroupError("创建 Agent 小镇群失败：响应缺少 chat_id")

    if remaining_role_apps:
        add_response = await client.post(
            f"{FEISHU_API_BASE}/im/v1/chats/{chat_id}/members",
            params={"member_id_type": "app_id"},
            headers=headers,
            json={"id_list": remaining_role_apps},
        )
        _response_payload(add_response, "补充角色机器人")

    return AgentTownGroup(chat_id=chat_id, created=True)


async def ensure_agent_town_group(
    *,
    controller_app_id: str,
    controller_app_secret: str,
    owner_id: str,
    role_app_ids: Sequence[str],
    data_dir: Path,
    owner_id_type: UserIdType = "open_id",
    client=None,
) -> AgentTownGroup:
    existing = load_agent_town_group(data_dir)
    if existing:
        remember_chat_id(data_dir, existing.chat_id)
        return existing
    if not owner_id.strip():
        raise ValueError("无法识别建群用户")
    if owner_id_type not in {"open_id", "union_id", "user_id"}:
        raise ValueError("不支持的飞书用户 ID 类型")
    if not role_app_ids:
        raise ValueError("至少需要配置一个角色机器人")

    if client is None:
        async with httpx.AsyncClient(timeout=20) as owned_client:
            group = await _provision_agent_town_group(
                client=owned_client,
                controller_app_id=controller_app_id,
                controller_app_secret=controller_app_secret,
                owner_id=owner_id,
                owner_id_type=owner_id_type,
                role_app_ids=role_app_ids,
            )
    else:
        group = await _provision_agent_town_group(
            client=client,
            controller_app_id=controller_app_id,
            controller_app_secret=controller_app_secret,
            owner_id=owner_id,
            owner_id_type=owner_id_type,
            role_app_ids=role_app_ids,
        )

    _save_agent_town_group(data_dir, group)
    remember_chat_id(data_dir, group.chat_id)
    return group
