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
    model: str | None = Field(default=None, min_length=2, max_length=200)
    workflow_stage: Literal["auto", "context", "action", "judge"] = "auto"
    enabled: bool = True
    timeout_seconds: float = Field(default=45.0, ge=1.0, le=900.0)
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
    model: str | None = Field(default=None, min_length=2, max_length=200)
    workflow_stage: Literal["auto", "context", "action", "judge"] | None = None
    enabled: bool | None = None
    timeout_seconds: float | None = Field(default=None, ge=1.0, le=900.0)
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


#: The four formats Claude accepts as image input. Anything else has to be
#: converted before it gets here, so an unsupported screenshot fails at the
#: entry point with a readable message instead of as a provider 400.
ImageMediaType = Literal["image/jpeg", "image/png", "image/gif", "image/webp"]

#: Base64 is ~4/3 of the raw bytes, and Claude rejects images over 5 MB. Cap
#: the encoded string instead of the decoded one: that is the value that
#: actually travels, and it keeps a 40 MB paste out of the request body.
MAX_IMAGE_BASE64_CHARS = 7_000_000


class ImageAttachment(BaseModel):
    """One picture the user attached, already base64 encoded.

    ``source_name`` is only used in receipts ("读了 jd.png") — it never enters a
    prompt, because a Feishu ``file_key`` or a user's filename is not evidence
    about the job.
    """

    media_type: ImageMediaType = "image/png"
    data: str = Field(min_length=8, max_length=MAX_IMAGE_BASE64_CHARS)
    source_name: str = Field(default="", max_length=200)

    def to_anthropic_block(self) -> dict[str, object]:
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": self.media_type,
                "data": self.data,
            },
        }

    def to_data_url(self) -> str:
        """The OpenAI-compatible spelling of the same picture."""

        return f"data:{self.media_type};base64,{self.data}"


class RunRequest(BaseModel):
    query: str = Field(min_length=2, max_length=20_000)
    requested_roles: list[str] = Field(default_factory=list)
    mode: Literal["single", "sequential", "parallel", "collaborative"] = "parallel"
    use_judge: bool = True
    #: Pictures every working role of this run may look at. The chief of staff
    #: usually empties this after reading them once (see ``chief_of_staff``),
    #: so a screenshot is not billed as image tokens by seven roles in a row.
    #: Capped at 9 to match one Feishu media batch.
    images: list[ImageAttachment] = Field(default_factory=list, max_length=9)
    #: Per-run override of every selected role's own timeout. A background
    #: patrol has nobody waiting on it, so it can afford the multi-round tool
    #: loop that a Feishu reply cannot.
    timeout_seconds: float | None = Field(default=None, ge=1.0, le=900.0)
    #: Who asked for this run. The page has exactly one "current run" slot, and
    #: the background keeps firing runs into it: a patrol every few minutes, the
    #: daily push once a morning. Without this they sort to the front and shove
    #: the collaborative run the user is watching off the screen. While such a
    #: run is still in flight nothing else tells it apart from a one-role request
    #: typed by hand, so the caller has to say. Anything other than ``user`` is
    #: background work and stays out of the header.
    origin: Literal["user", "patrol", "schedule"] = "user"


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
