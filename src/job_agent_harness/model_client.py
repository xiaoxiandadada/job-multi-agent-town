from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Protocol

import httpx

from .models import ModelReply, RoleSpec
from .role_context import build_role_context, default_prepare_dir


class ModelClient(Protocol):
    async def complete(self, role: RoleSpec, query: str) -> ModelReply: ...


class OpenAICompatibleClient:
    """Small provider-neutral client for OpenAI-compatible chat APIs."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        default_model: str | None = None,
        judge_model: str | None = None,
        knowledge_model: str | None = None,
        reliable_model: str | None = None,
        max_output_tokens: int | None = None,
        project_context_path: str | Path | None = None,
        prepare_dir: str | Path | None = None,
        role_context_max_chars: int | None = None,
        seed_roles_path: str | Path | None = None,
    ):
        self.base_url = (base_url or os.getenv("JOB_AGENT_API_BASE", "")).rstrip("/")
        self.api_key = api_key or os.getenv("JOB_AGENT_API_KEY", "")
        self.default_model = default_model or os.getenv("JOB_AGENT_MODEL", "")
        self.judge_model = judge_model or os.getenv(
            "JOB_AGENT_JUDGE_MODEL", self.default_model
        )
        self.knowledge_model = knowledge_model or os.getenv(
            "JOB_AGENT_KNOWLEDGE_MODEL", self.judge_model
        )
        self.reliable_model = reliable_model or os.getenv(
            "JOB_AGENT_RELIABLE_MODEL", self.judge_model
        )
        self.max_output_tokens = max_output_tokens or int(
            os.getenv("JOB_AGENT_MAX_OUTPUT_TOKENS", "800")
        )
        configured_context_path = project_context_path or os.getenv(
            "JOB_AGENT_PROJECT_CONTEXT_FILE",
            Path(__file__).resolve().parents[2]
            / "configs"
            / "project-context.md",
        )
        self.project_context_path = Path(configured_context_path)
        self.prepare_dir = Path(prepare_dir) if prepare_dir else default_prepare_dir()
        self.role_context_max_chars = role_context_max_chars or int(
            os.getenv("JOB_AGENT_ROLE_CONTEXT_MAX_CHARS", "18000")
        )
        configured_seed_path = seed_roles_path or (
            Path(__file__).resolve().parents[2] / "configs" / "roles.json"
        )
        self.seed_roles_path = Path(configured_seed_path)

    def model_for(self, role: RoleSpec) -> str:
        if role.model:
            return role.model
        role_env = "JOB_AGENT_ROLE_MODEL_" + re.sub(
            r"[^A-Z0-9]+",
            "_",
            role.role_id.upper(),
        ).strip("_")
        if os.getenv(role_env):
            return os.environ[role_env]
        return {
            "default": self.default_model,
            "judge": self.judge_model,
            "knowledge": self.knowledge_model,
            "reliable": self.reliable_model,
        }.get(role.model_profile, self.default_model)

    def profile_models(self) -> dict[str, str]:
        return {
            "default": self.default_model,
            "reliable": self.reliable_model,
            "knowledge": self.knowledge_model,
            "judge": self.judge_model,
        }

    def _managed_prompt_for(self, role: RoleSpec) -> str:
        if not self.seed_roles_path.is_file():
            return role.system_prompt.strip()
        try:
            seed_roles = json.loads(
                self.seed_roles_path.read_text(encoding="utf-8")
            )
        except (ValueError, OSError):
            return role.system_prompt.strip()
        seed_prompt = next(
            (
                item.get("system_prompt", "").strip()
                for item in seed_roles
                if item.get("role_id") == role.role_id
            ),
            "",
        )
        if not seed_prompt or seed_prompt == role.system_prompt.strip():
            return role.system_prompt.strip()
        return (
            f"{seed_prompt}\n\n"
            "# 运行时附加指令\n"
            f"{role.system_prompt.strip()}"
        )

    def system_prompt_for(self, role: RoleSpec) -> str:
        """Ground every specialist in managed instructions and local evidence."""

        prompt = self._managed_prompt_for(role)
        needs_local_context = (
            "local_docs" in role.tools
            or "web_search" in role.tools
            or role.role_id == "judge"
        )
        context_blocks: list[str] = []
        if needs_local_context and self.project_context_path.is_file():
            project_context = self.project_context_path.read_text(
                encoding="utf-8"
            ).strip()
            if project_context:
                context_blocks.append(
                    "# 已核验的本地项目事实\n"
                    f"{project_context}"
                )
        if needs_local_context:
            role_context = build_role_context(
                role,
                prepare_dir=self.prepare_dir,
                max_chars=self.role_context_max_chars,
            )
            if role_context:
                context_blocks.append(
                    "# 该角色的最新求职资料包\n"
                    "这些内容来自本地日报、岗位表、学习计划、简历或作品路线。"
                    "时间敏感信息只可按文件中明确日期使用；没有官方链接时标待核验。\n\n"
                    f"{role_context}"
                )
        if not context_blocks:
            return prompt
        return (
            f"{prompt}\n\n"
            "# 证据使用规则\n"
            "回答必须覆盖本角色负责范围，并给出结论、依据、风险与下一步。"
            "不得补写资料中没有出现的公司要求、框架、指标、PR 或部署状态；"
            "无法由用户输入或资料包支持的说法必须删除或标为“待核验”。\n\n"
            + "\n\n".join(context_blocks)
        )

    async def available_models(self) -> list[str]:
        if not self.base_url or not self.api_key:
            return []
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                f"{self.base_url}/models",
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
            response.raise_for_status()
            payload = response.json()
        return sorted(
            {
                item["id"]
                for item in payload.get("data", [])
                if isinstance(item, dict) and item.get("id")
            }
        )

    async def complete(self, role: RoleSpec, query: str) -> ModelReply:
        if not self.base_url or not self.api_key or not self.default_model:
            raise RuntimeError(
                "model API is not configured; set JOB_AGENT_API_BASE, "
                "JOB_AGENT_API_KEY and JOB_AGENT_MODEL"
            )
        model = self.model_for(role)
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": self.system_prompt_for(role),
                },
                {"role": "user", "content": query},
            ],
            "temperature": 0.2,
            "max_tokens": self.max_output_tokens,
        }
        async with httpx.AsyncClient(timeout=role.timeout_seconds) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
        usage = data.get("usage", {})
        return ModelReply(
            content=data["choices"][0]["message"]["content"],
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            model=data.get("model", model),
        )


class MockModelClient:
    def __init__(self, latency_seconds: float = 0):
        self.latency_seconds = latency_seconds
        self.calls: list[str] = []
        self.queries: list[tuple[str, str]] = []

    async def complete(self, role: RoleSpec, query: str) -> ModelReply:
        import asyncio

        self.calls.append(role.role_id)
        self.queries.append((role.role_id, query))
        if self.latency_seconds:
            await asyncio.sleep(self.latency_seconds)
        return ModelReply(
            content=f"[{role.display_name}] {query}",
            input_tokens=len(query),
            output_tokens=8,
            model="mock",
        )
