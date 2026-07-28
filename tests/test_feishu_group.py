import json

import httpx
import pytest

from job_agent_harness.daily_brief import load_chat_id, remember_chat_id
from job_agent_harness.feishu_group import (
    AGENT_TOWN_NAME,
    configured_role_app_ids,
    ensure_agent_town_group,
    load_agent_town_group,
    remember_preferred_chat_id,
)


class FakeAsyncClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        response.request = httpx.Request("POST", url)
        return response


def response(payload, status=200):
    return httpx.Response(status, json=payload)


@pytest.mark.asyncio
async def test_create_group_adds_first_five_then_remaining_two(tmp_path):
    role_app_ids = [f"cli_role_{index}" for index in range(7)]
    client = FakeAsyncClient(
        [
            response({"code": 0, "tenant_access_token": "token"}),
            response({"code": 0, "data": {"chat_id": "oc_town"}}),
            response({"code": 0, "data": {}}),
        ]
    )

    group = await ensure_agent_town_group(
        controller_app_id="cli_controller",
        controller_app_secret="secret",
        owner_open_id="ou_owner",
        role_app_ids=role_app_ids,
        data_dir=tmp_path,
        client=client,
    )

    assert group.chat_id == "oc_town"
    assert group.created is True
    assert client.calls[1][1]["json"]["name"] == AGENT_TOWN_NAME
    assert client.calls[1][1]["json"]["owner_id"] == "ou_owner"
    assert client.calls[1][1]["json"]["bot_id_list"] == role_app_ids[:5]
    assert client.calls[2][1]["params"] == {
        "member_id_type": "app_id"
    }
    assert client.calls[2][1]["json"]["id_list"] == role_app_ids[5:]
    assert load_chat_id(tmp_path) == "oc_town"
    assert load_agent_town_group(tmp_path).chat_id == "oc_town"


@pytest.mark.asyncio
async def test_existing_group_is_idempotent_and_restores_target(tmp_path):
    (tmp_path / "feishu_group.json").write_text(
        json.dumps({"chat_id": "oc_existing", "name": AGENT_TOWN_NAME}),
        encoding="utf-8",
    )
    remember_chat_id(tmp_path, "oc_private")
    client = FakeAsyncClient([])

    group = await ensure_agent_town_group(
        controller_app_id="cli_controller",
        controller_app_secret="secret",
        owner_open_id="ou_owner",
        role_app_ids=["cli_role"],
        data_dir=tmp_path,
        client=client,
    )

    assert group.chat_id == "oc_existing"
    assert group.created is False
    assert client.calls == []
    assert load_chat_id(tmp_path) == "oc_existing"


@pytest.mark.asyncio
async def test_member_add_failure_does_not_replace_previous_target(tmp_path):
    remember_chat_id(tmp_path, "oc_private")
    client = FakeAsyncClient(
        [
            response({"code": 0, "tenant_access_token": "token"}),
            response({"code": 0, "data": {"chat_id": "oc_partial"}}),
            response({"code": 230001, "msg": "permission denied"}),
        ]
    )

    with pytest.raises(RuntimeError, match="补充角色机器人失败"):
        await ensure_agent_town_group(
            controller_app_id="cli_controller",
            controller_app_secret="secret",
            owner_open_id="ou_owner",
            role_app_ids=[f"cli_role_{index}" for index in range(7)],
            data_dir=tmp_path,
            client=client,
        )

    assert load_chat_id(tmp_path) == "oc_private"
    assert load_agent_town_group(tmp_path) is None


def test_preferred_chat_stays_pinned_after_group_creation(tmp_path):
    (tmp_path / "feishu_group.json").write_text(
        json.dumps({"chat_id": "oc_town", "name": AGENT_TOWN_NAME}),
        encoding="utf-8",
    )

    selected = remember_preferred_chat_id(tmp_path, "oc_private")

    assert selected == "oc_town"
    assert load_chat_id(tmp_path) == "oc_town"


def test_configured_role_app_ids_reads_all_roles_in_order():
    env = {
        "ROLE_SCOUT_ID": "cli_scout",
        "ROLE_JUDGE_ID": "cli_judge",
    }

    app_ids = configured_role_app_ids(
        ["scout", "judge"],
        env,
        lambda role_id: (
            f"ROLE_{role_id.upper()}_ID",
            f"ROLE_{role_id.upper()}_SECRET",
        ),
    )

    assert app_ids == ["cli_scout", "cli_judge"]
