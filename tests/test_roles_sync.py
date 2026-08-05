import json

from job_agent_harness.registry import RoleRegistry
from job_agent_harness.roles_sync import plan_sync, sync_roles


def seed_payload(**changes):
    role = {
        "role_id": "job_scout",
        "display_name": "Job Scout",
        "goal": "发现并核验中国 2027 届正式校招岗位",
        "system_prompt": "逐条给出公司、岗位、官方链接和 JD 原文引用。",
        "tools": ["web_search"],
        "model_profile": "reliable",
        "timeout_seconds": 170.0,
    }
    role.update(changes)
    return [role]


def write_seed(tmp_path, roles):
    path = tmp_path / "roles.json"
    path.write_text(json.dumps(roles, ensure_ascii=False), encoding="utf-8")
    return path


def test_sync_pushes_new_prompts_and_timeouts_into_a_stale_registry(tmp_path):
    seed = write_seed(tmp_path, seed_payload())
    registry = RoleRegistry.from_seed(tmp_path / "runtime.json", seed)
    registry.update(
        "job_scout",
        timeout_seconds=60.0,
        system_prompt="旧提示词，只给抽象清单。",
    )

    changes, version = sync_roles(registry, seed)
    role = registry.get("job_scout")

    assert changes == {"job_scout": ["system_prompt", "timeout_seconds"]}
    assert role.timeout_seconds == 170.0
    assert "JD 原文引用" in role.system_prompt
    assert version == registry.version


def test_sync_preserves_runtime_owned_fields(tmp_path):
    seed = write_seed(tmp_path, seed_payload())
    registry = RoleRegistry.from_seed(tmp_path / "runtime.json", seed)
    registry.update("job_scout", enabled=False, model="claude-opus-5")

    sync_roles(registry, seed)
    role = registry.get("job_scout")

    assert role.enabled is False
    assert role.model == "claude-opus-5"
    assert role.timeout_seconds == 170.0


def test_sync_adds_roles_that_only_exist_in_the_seed(tmp_path):
    seed = write_seed(tmp_path, seed_payload())
    registry = RoleRegistry.from_seed(tmp_path / "runtime.json", seed)
    extended = seed_payload() + [
        {
            "role_id": "voice_coach",
            "display_name": "Voice Coach",
            "goal": "用语音跟用户做多轮模拟面试",
            "system_prompt": "一次只问一个问题，等用户回答。",
        }
    ]
    seed = write_seed(tmp_path, extended)

    changes, _ = sync_roles(registry, seed)

    assert changes["voice_coach"] == ["新增角色"]
    assert registry.get("voice_coach").display_name == "Voice Coach"


def test_dry_run_plan_reports_changes_without_writing(tmp_path):
    seed = write_seed(tmp_path, seed_payload())
    registry = RoleRegistry.from_seed(tmp_path / "runtime.json", seed)
    registry.update("job_scout", timeout_seconds=60.0)
    before = registry.version

    _, changes = plan_sync(registry, seed)

    assert changes == {"job_scout": ["timeout_seconds"]}
    assert registry.version == before
    assert registry.get("job_scout").timeout_seconds == 60.0


def test_sync_is_idempotent(tmp_path):
    seed = write_seed(tmp_path, seed_payload())
    registry = RoleRegistry.from_seed(tmp_path / "runtime.json", seed)
    registry.update("job_scout", timeout_seconds=60.0)

    sync_roles(registry, seed)
    version_after_first = registry.version
    changes, version = sync_roles(registry, seed)

    assert changes == {}
    assert version == version_after_first
