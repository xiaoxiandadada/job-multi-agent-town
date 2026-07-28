from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol

import httpx

from .models import ModelReply, RoleSpec


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

    def model_for(self, role: RoleSpec) -> str:
        return {
            "default": self.default_model,
            "judge": self.judge_model,
            "knowledge": self.knowledge_model,
            "reliable": self.reliable_model,
        }.get(role.model_profile, self.default_model)

    def system_prompt_for(self, role: RoleSpec) -> str:
        """Ground local-document roles in a small, reviewable fact sheet."""

        prompt = role.system_prompt.strip()
        needs_local_context = (
            "local_docs" in role.tools or role.role_id == "judge"
        )
        if not needs_local_context or not self.project_context_path.is_file():
            return prompt
        context = self.project_context_path.read_text(
            encoding="utf-8"
        ).strip()
        if not context:
            return prompt
        return (
            f"{prompt}\n\n"
            "# 已核验的本地项目上下文\n"
            "以下事实来自随代码版本管理的项目事实表。"
            "仅在问题与本项目相关时使用；不得补写其中没有出现的框架、"
            "指标、PR、部署状态或功能。无法由用户输入或事实表支持的说法，"
            "必须删除或明确标为“待核验”。\n\n"
            f"{context}"
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
