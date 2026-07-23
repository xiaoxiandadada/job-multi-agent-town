from __future__ import annotations

import os
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
    ):
        self.base_url = (base_url or os.getenv("JOB_AGENT_API_BASE", "")).rstrip("/")
        self.api_key = api_key or os.getenv("JOB_AGENT_API_KEY", "")
        self.default_model = default_model or os.getenv("JOB_AGENT_MODEL", "")
        self.judge_model = judge_model or os.getenv(
            "JOB_AGENT_JUDGE_MODEL", self.default_model
        )

    async def complete(self, role: RoleSpec, query: str) -> ModelReply:
        if not self.base_url or not self.api_key or not self.default_model:
            raise RuntimeError(
                "model API is not configured; set JOB_AGENT_API_BASE, "
                "JOB_AGENT_API_KEY and JOB_AGENT_MODEL"
            )
        model = (
            self.judge_model if role.model_profile == "judge" else self.default_model
        )
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": role.system_prompt},
                {"role": "user", "content": query},
            ],
            "temperature": 0.2,
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

    async def complete(self, role: RoleSpec, query: str) -> ModelReply:
        import asyncio

        self.calls.append(role.role_id)
        if self.latency_seconds:
            await asyncio.sleep(self.latency_seconds)
        return ModelReply(
            content=f"[{role.display_name}] {query}",
            input_tokens=len(query),
            output_tokens=8,
            model="mock",
        )

