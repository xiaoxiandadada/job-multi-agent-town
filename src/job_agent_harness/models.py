from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class RoleSpec(BaseModel):
    role_id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,48}$")
    display_name: str = Field(min_length=2, max_length=40)
    goal: str = Field(min_length=8, max_length=300)
    system_prompt: str = Field(min_length=8, max_length=4000)
    trigger_keywords: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    model_profile: str = "default"
    workflow_stage: Literal["auto", "context", "action", "judge"] = "auto"
    enabled: bool = True
    timeout_seconds: float = Field(default=45.0, ge=1.0, le=180.0)
    town_place: str | None = Field(default=None, min_length=2, max_length=40)
    town_icon: str = Field(default="🏠", min_length=1, max_length=8)
    town_x: float | None = Field(default=None, ge=4, le=96)
    town_y: float | None = Field(default=None, ge=8, le=88)
    schedule: list[str] = Field(default_factory=list)

    @field_validator("trigger_keywords", "tools", "schedule")
    @classmethod
    def unique_items(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(item.strip() for item in value if item.strip()))


class RolePatch(BaseModel):
    display_name: str | None = Field(default=None, min_length=2, max_length=40)
    goal: str | None = Field(default=None, min_length=8, max_length=300)
    system_prompt: str | None = Field(
        default=None,
        min_length=8,
        max_length=4000,
    )
    trigger_keywords: list[str] | None = None
    tools: list[str] | None = None
    model_profile: str | None = None
    workflow_stage: Literal["auto", "context", "action", "judge"] | None = None
    enabled: bool | None = None
    timeout_seconds: float | None = Field(default=None, ge=1.0, le=180.0)
    town_place: str | None = Field(default=None, min_length=2, max_length=40)
    town_icon: str | None = Field(default=None, min_length=1, max_length=8)
    town_x: float | None = Field(default=None, ge=4, le=96)
    town_y: float | None = Field(default=None, ge=8, le=88)
    schedule: list[str] | None = None

    @field_validator("trigger_keywords", "tools", "schedule")
    @classmethod
    def unique_optional_items(
        cls,
        value: list[str] | None,
    ) -> list[str] | None:
        if value is None:
            return None
        return list(dict.fromkeys(item.strip() for item in value if item.strip()))


class RunRequest(BaseModel):
    query: str = Field(min_length=2, max_length=20_000)
    requested_roles: list[str] = Field(default_factory=list)
    mode: Literal["single", "sequential", "parallel", "collaborative"] = "parallel"
    use_judge: bool = True


class ModelReply(BaseModel):
    content: str
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = "unknown"


class AgentResult(BaseModel):
    role_id: str
    display_name: str
    output: str = ""
    status: Literal["ok", "error", "timeout"] = "ok"
    latency_ms: float = 0
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = "unknown"
    error: str | None = None


class RunMetrics(BaseModel):
    selected_roles: int
    completed_roles: int
    failed_roles: int
    model_calls: int
    wall_latency_ms: float
    sum_agent_latency_ms: float
    input_tokens: int
    output_tokens: int
    parallel_speedup_estimate: float


class RunReport(BaseModel):
    run_id: str
    query: str
    mode: str
    role_registry_version: int
    results: list[AgentResult]
    final_output: str
    metrics: RunMetrics
