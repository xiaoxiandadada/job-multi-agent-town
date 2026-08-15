"""Re-sync managed role fields from the seed config into the runtime registry.

``RoleRegistry.from_seed`` deliberately never overwrites a role that already
exists at runtime, so edits made through ``PATCH /api/roles`` survive a
restart. The cost is that editing ``configs/roles.json`` — new prompts, new
timeouts — has no effect on an existing deployment. This command closes that
gap explicitly: it pushes the seed's *operational* fields and preserves the
two fields a human actually tunes per environment (``enabled`` and the
per-role ``model`` override).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .models import RoleSpec
from .registry import RoleRegistry
from .runtime import ROOT, build_registry


#: Fields owned by ``configs/roles.json`` once a sync is requested.
SEED_MANAGED_FIELDS = (
    "display_name",
    "goal",
    "system_prompt",
    "trigger_keywords",
    "tools",
    "model_profile",
    "effort",
    "workflow_stage",
    "timeout_seconds",
    "town_place",
    "town_icon",
    "town_x",
    "town_y",
    "schedule",
)

#: Never touched by a sync; these belong to the running environment.
RUNTIME_OWNED_FIELDS = ("enabled", "model")


def seed_roles(seed_path: Path) -> list[RoleSpec]:
    return [
        RoleSpec.model_validate(item)
        for item in json.loads(seed_path.read_text(encoding="utf-8"))
    ]


def plan_sync(
    registry: RoleRegistry,
    seed_path: Path,
    fields: tuple[str, ...] = SEED_MANAGED_FIELDS,
    *,
    disable_missing: bool = False,
) -> tuple[list[RoleSpec], dict[str, list[str]]]:
    """Return the merged role list plus, per role, which fields changed.

    ``disable_missing`` handles the case the default policy cannot: a role that
    was *removed* from the seed (merged into another one, retired) otherwise
    stays enabled at runtime and keeps getting dispatched, because the registry
    has no delete and a plain sync only touches roles the seed still lists.
    Disabling rather than deleting keeps its past runs readable on the timeline.
    """

    seeds = {role.role_id: role for role in seed_roles(seed_path)}
    merged: list[RoleSpec] = []
    changes: dict[str, list[str]] = {}
    for role in registry.list_roles(include_disabled=True):
        seed = seeds.pop(role.role_id, None)
        if seed is None:
            if disable_missing and role.enabled:
                changes[role.role_id] = ["seed 已删除，禁用"]
                merged.append(role.model_copy(update={"enabled": False}))
            else:
                merged.append(role)
            continue
        updates = {
            field: getattr(seed, field)
            for field in fields
            if getattr(seed, field) != getattr(role, field)
        }
        if updates:
            changes[role.role_id] = sorted(updates)
        merged.append(role.model_copy(update=updates))
    for role_id, seed in seeds.items():
        changes[role_id] = ["新增角色"]
        merged.append(seed)
    return merged, changes


def sync_roles(
    registry: RoleRegistry,
    seed_path: Path,
    fields: tuple[str, ...] = SEED_MANAGED_FIELDS,
    *,
    disable_missing: bool = False,
) -> tuple[dict[str, list[str]], int]:
    merged, changes = plan_sync(
        registry,
        seed_path,
        fields,
        disable_missing=disable_missing,
    )
    if not changes:
        return changes, registry.version
    return changes, registry.replace_all(merged)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "把 configs/roles.json 的提示词、超时和小镇布局同步到运行态 registry；"
            f"保留 {', '.join(RUNTIME_OWNED_FIELDS)}"
        )
    )
    parser.add_argument(
        "--seed",
        default=str(ROOT / "configs" / "roles.json"),
        help="seed 配置路径",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只显示将要变更的字段，不写入",
    )
    parser.add_argument(
        "--disable-missing",
        action="store_true",
        help=(
            "把 seed 里已经删除的角色设为 enabled=false。"
            "合并或下线角色后需要这个，否则旧角色会留在运行态注册表里继续被派单。"
        ),
    )
    args = parser.parse_args()

    registry = build_registry()
    seed_path = Path(args.seed)
    if args.dry_run:
        _, changes = plan_sync(
            registry,
            seed_path,
            disable_missing=args.disable_missing,
        )
        version = registry.version
    else:
        changes, version = sync_roles(
            registry,
            seed_path,
            disable_missing=args.disable_missing,
        )

    if not changes:
        print(f"registry 已与 seed 一致（version={version}）")
        return
    verb = "将更新" if args.dry_run else "已更新"
    for role_id in sorted(changes):
        print(f"{verb} {role_id}: {', '.join(changes[role_id])}")
    print(f"registry version={version}")


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    main()
