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

