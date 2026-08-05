import json

import pytest

from job_agent_harness.models import RoleSpec
from job_agent_harness.registry import RoleRegistry


def make_role(role_id: str = "test_role") -> RoleSpec:
    return RoleSpec(
        role_id=role_id,
        display_name="测试角色",
        goal="验证动态添加角色能立即生效",
        system_prompt="输出结构化、可验证的测试结果。",
        trigger_keywords=["测试"],
    )


def test_add_role_is_persisted_and_versioned(tmp_path):
    registry = RoleRegistry(tmp_path / "roles.json")

    version = registry.add(make_role())

    assert version == 1
    assert registry.version == 1
    assert registry.get("test_role").display_name == "测试角色"
    assert json.loads((tmp_path / "roles.json").read_text())["version"] == 1


def test_update_role_is_persisted_and_versioned(tmp_path):
    registry = RoleRegistry(tmp_path / "roles.json")
    registry.add(make_role())

    role, version = registry.update(
        "test_role",
        enabled=False,
        town_place="测试工坊",
        workflow_stage="context",
    )

    assert version == 2
    assert role.enabled is False
    assert registry.get("test_role").town_place == "测试工坊"
    assert registry.get("test_role").workflow_stage == "context"
    assert json.loads((tmp_path / "roles.json").read_text())["version"] == 2


def test_duplicate_role_is_rejected(tmp_path):
    registry = RoleRegistry(tmp_path / "roles.json")
    registry.add(make_role())

    with pytest.raises(ValueError, match="already exists"):
        registry.add(make_role())


def test_from_seed_adds_missing_seed_roles_without_removing_custom_roles(tmp_path):
    runtime_path = tmp_path / "runtime-roles.json"
    seed_path = tmp_path / "seed-roles.json"
    registry = RoleRegistry(runtime_path)
    registry.add(make_role("custom_role"))
    initial_version = registry.version
    seed_path.write_text(
        json.dumps(
            [make_role("seed_role").model_dump()],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    merged = RoleRegistry.from_seed(runtime_path, seed_path)

    assert {role.role_id for role in merged.list_roles()} == {
        "custom_role",
        "seed_role",
    }
    assert merged.version == initial_version + 1


def test_from_seed_backfills_new_town_fields_without_overwriting_user_fields(
    tmp_path,
):
    runtime_path = tmp_path / "runtime-roles.json"
    seed_path = tmp_path / "seed-roles.json"
    runtime_path.write_text(
        json.dumps(
            {
                "version": 4,
                "roles": [
                    {
                        "role_id": "job_scout",
                        "display_name": "用户自定义侦察员",
                        "goal": "发现并核验符合范围的岗位机会",
                        "system_prompt": "只输出有官方来源且范围明确的岗位。",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    seeded = make_role("job_scout").model_copy(
        update={
            "display_name": "种子名称",
            "workflow_stage": "context",
            "town_place": "Scout Outpost",
            "town_icon": "📡",
            "schedule": ["核验岗位", "检查来源"],
        }
    )
    seed_path.write_text(
        json.dumps([seeded.model_dump()], ensure_ascii=False),
        encoding="utf-8",
    )

    registry = RoleRegistry.from_seed(runtime_path, seed_path)
    role = registry.get("job_scout")

    assert role.display_name == "用户自定义侦察员"
    assert role.workflow_stage == "context"
    assert role.town_place == "Scout Outpost"
    assert role.schedule == ["核验岗位", "检查来源"]
