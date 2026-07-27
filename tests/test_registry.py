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
