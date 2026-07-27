from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .models import RoleSpec


class RoleRegistry:
    """Versioned role registry that can be updated without restarting the app."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write({"version": 0, "roles": []})

    @classmethod
    def from_seed(cls, runtime_path: Path, seed_path: Path) -> "RoleRegistry":
        registry = cls(runtime_path)
        seed_roles = [
            RoleSpec.model_validate(item)
            for item in json.loads(seed_path.read_text(encoding="utf-8"))
        ]
        existing_roles = registry.list_roles(include_disabled=True)
        existing_ids = {role.role_id for role in existing_roles}
        missing_seed_roles = [
            role for role in seed_roles if role.role_id not in existing_ids
        ]
        if missing_seed_roles:
            registry.replace_all([*existing_roles, *missing_seed_roles])
        return registry

    def _read(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _write(self, payload: dict) -> None:
        serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        fd, tmp_name = tempfile.mkstemp(
            dir=self.path.parent, prefix=f".{self.path.name}.", text=True
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(serialized)
            os.replace(tmp_name, self.path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    @property
    def version(self) -> int:
        return int(self._read()["version"])

    def list_roles(self, include_disabled: bool = False) -> list[RoleSpec]:
        roles = [RoleSpec.model_validate(item) for item in self._read()["roles"]]
        return roles if include_disabled else [role for role in roles if role.enabled]

    def get(self, role_id: str) -> RoleSpec:
        for role in self.list_roles(include_disabled=True):
            if role.role_id == role_id:
                return role
        raise KeyError(f"unknown role: {role_id}")

    def add(self, role: RoleSpec) -> int:
        payload = self._read()
        if any(item["role_id"] == role.role_id for item in payload["roles"]):
            raise ValueError(f"role already exists: {role.role_id}")
        payload["roles"].append(role.model_dump())
        payload["version"] += 1
        self._write(payload)
        return payload["version"]

    def replace_all(self, roles: list[RoleSpec]) -> int:
        role_ids = [role.role_id for role in roles]
        if len(role_ids) != len(set(role_ids)):
            raise ValueError("duplicate role ids")
        payload = {
            "version": self.version + 1,
            "roles": [role.model_dump() for role in roles],
        }
        self._write(payload)
        return payload["version"]
