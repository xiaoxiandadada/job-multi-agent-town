from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


#: How hard the model should work on one call. Claude 5 reads this as
#: ``output_config.effort``; leaving it unset sends no ``output_config`` at all,
#: which is not the same as sending ``high``: Haiku 4.5 rejects the parameter
#: outright, so a role pinned to Haiku has to be able to opt out rather than
#: inherit a value from the deployment default.
EffortLevel = Literal["low", "medium", "high", "xhigh", "max"]


class RoleSpec(BaseModel):
    role_id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,48}$")
    display_name: str = Field(min_length=2, max_length=40)
    goal: str = Field(min_length=8, max_length=300)
    system_prompt: str = Field(min_length=8, max_length=4000)
    trigger_keywords: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    model_profile: str = "default"
    model: str | None = Field(default=None, min_length=2, max_length=200)
    #: Per-role reasoning depth. ``None`` inherits the deployment-wide default
    #: (``JOB_AGENT_CLAUDE_EFFORT``), which itself defaults to sending nothing.
    #: This is a finer lever than swapping models: a strong model at ``medium``
    #: often beats a weaker one at ``high``, and it costs less.
    effort: EffortLevel | None = None
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
    effort: EffortLevel | None = None
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


#: How many specialists one run may wake up. The old keyword router had no
#: ceiling at all: overlapping ``trigger_keywords`` meant "看看这个岗位的 JD"
#: matched three roles, each of which wrote a full-length answer the Judge then
#: had to merge. A planner that must choose earns a much shorter, more specific
#: run, so the cap is enforced here and not left to the prompt.
MAX_PLANNED_SUBTASKS = 3


class PlannedSubtask(BaseModel):
    """One specialist, and what this run actually needs from it."""

    role_id: str
    #: What this role should do for *this* query, in the planner's words. Beats
    #: the role's generic ``goal`` as a task-graph node title.
    task: str = Field(min_length=2, max_length=600)
    #: Why the run needs this role. Shown in the dispatch receipt so the routing
    #: decision is legible instead of a keyword table the user cannot see.
    why: str = Field(default="", max_length=300)
    #: Other ``role_id``s whose evidence this one should build on. The planner
    #: decides the shape; the orchestrator no longer hardcodes it per phase.
    depends_on: list[str] = Field(default_factory=list)


class RunPlan(BaseModel):
    """A decomposition of one user request into the fewest roles that serve it.

    ``source`` records how the plan was reached, because the planner is allowed
    to fail. A malformed model reply must degrade to the keyword router rather
    than take the run down with it, and the activity timeline should say which
    of the two actually decided.
    """

    intent: str = Field(default="", max_length=600)
    subtasks: list[PlannedSubtask] = Field(default_factory=list)
    #: Whether the Evidence Judge is worth its own call. One specialist
    #: answering a direct question does not need an audit pass appended to it.
    need_judge: bool = True
    #: Which capable roles were deliberately left out, and why. Kept so a user
    #: who expected six answers can see the run chose not to, on purpose.
    skipped: str = Field(default="", max_length=600)
    source: Literal["planner", "keywords", "explicit"] = "planner"

    @field_validator("subtasks")
    @classmethod
    def cap_and_dedupe(cls, value: list[PlannedSubtask]) -> list[PlannedSubtask]:
        """Enforce the ceiling in code, not just in the prompt.

        A model asked for "at most three" will occasionally return five. Trust
        the field, never the instruction.
        """

        unique: dict[str, PlannedSubtask] = {}
        for subtask in value:
            unique.setdefault(subtask.role_id, subtask)
        return list(unique.values())[:MAX_PLANNED_SUBTASKS]

    def role_ids(self) -> list[str]:
        return [subtask.role_id for subtask in self.subtasks]


class RunRequest(BaseModel):
    query: str = Field(min_length=2, max_length=20_000)
    requested_roles: list[str] = Field(default_factory=list)
    mode: Literal["single", "sequential", "parallel", "collaborative"] = "parallel"
    use_judge: bool = True
    #: The Chief of Staff's decomposition, when this run came through it. The
    #: orchestrator prefers it over ``trigger_keywords``. Absent for the direct
    #: callers (always-on patrol, daily push, mock interviews), which keep the
    #: keyword router — they already know which role they want.
    plan: RunPlan | None = None
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


# --------------------------------------------------------------- match scoring

#: The five dimensions every job is scored on. Fixed rather than model-chosen so
#: two jobs are actually comparable: a model that invents its own axes per job
#: produces numbers that look like data and cannot be ranked against each other.
MATCH_DIMENSIONS = (
    "硬性门槛",
    "核心技能",
    "项目相关性",
    "加分项",
    "投递时效",
)


class MatchDimension(BaseModel):
    """One axis of a match score, with the evidence that justifies it.

    ``evidence`` is required and load-bearing: a score with no citable basis is
    the failure mode this whole project exists to prevent, and a number is a
    much better disguise for it than prose is.
    """

    name: str = Field(min_length=2, max_length=20)
    score: int = Field(ge=0, le=100)
    weight: float = Field(gt=0, le=1)
    evidence: str = Field(min_length=2, max_length=600)
    #: What is missing on this axis. Empty means nothing is.
    gap: str = Field(default="", max_length=600)


class MatchReport(BaseModel):
    """A comparable, evidence-backed fit score for one job.

    ``overall`` is **computed from the dimensions, not taken from the model** —
    see ``recomputed()``. Asking a model for both the parts and the total invites
    a total that does not follow from its own parts.
    """

    company: str = Field(min_length=1, max_length=80)
    job_title: str = Field(min_length=1, max_length=120)
    job_url: str = Field(default="", max_length=500)
    overall: int = Field(default=0, ge=0, le=100)
    verdict: Literal["值得投", "补强后投", "不建议"] = "补强后投"
    dimensions: list[MatchDimension] = Field(min_length=1, max_length=8)
    #: Hard stops — a 985/211 requirement, wrong graduation year. These make the
    #: overall score irrelevant, so they are surfaced separately rather than
    #: buried as a low score on one axis.
    blockers: list[str] = Field(default_factory=list, max_length=10)
    #: Things that would raise the score today. The point of the number is to
    #: drive an action, not to be admired.
    quick_wins: list[str] = Field(default_factory=list, max_length=10)

    def recomputed(self) -> "MatchReport":
        """Return a copy whose ``overall`` is the weighted mean of its parts.

        Weights are normalised rather than required to sum to 1: rejecting an
        otherwise good report over arithmetic the model got slightly wrong would
        cost more than renormalising it.
        """

        total_weight = sum(dimension.weight for dimension in self.dimensions)
        if total_weight <= 0:
            return self
        weighted = sum(
            dimension.score * dimension.weight for dimension in self.dimensions
        )
        overall = round(weighted / total_weight)
        # A hard blocker caps the score: "80% fit, but you are the wrong
        # graduation year" is not an 80.
        if self.blockers:
            overall = min(overall, 40)
        return self.model_copy(update={"overall": max(0, min(100, overall))})


#: Sent to the model as ``output_config.format``. Kept next to ``MatchReport``
#: because the two are one contract — a schema that drifts from the parser is
#: worse than no schema. Note the API's JSON-Schema subset: no ``minimum`` /
#: ``maximum`` (those are enforced by the Pydantic model above), and every object
#: needs ``additionalProperties: false``.
MATCH_REPORT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "company": {"type": "string", "description": "公司名，照抄 JD 原文"},
        "job_title": {"type": "string", "description": "岗位全名，照抄 JD 原文"},
        "job_url": {"type": "string", "description": "官方 JD 链接，没有就留空"},
        "verdict": {
            "type": "string",
            "enum": ["值得投", "补强后投", "不建议"],
            "description": "结论。有硬性阻断项时不能是“值得投”",
        },
        "dimensions": {
            "type": "array",
            "description": (
                "固定五个维度，顺序不限但必须齐全："
                + "、".join(MATCH_DIMENSIONS)
            ),
            "items": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "enum": list(MATCH_DIMENSIONS),
                    },
                    "score": {
                        "type": "integer",
                        "description": "0-100，这一维的匹配程度",
                    },
                    "weight": {
                        "type": "number",
                        "description": "该维度权重，五个加起来约等于 1",
                    },
                    "evidence": {
                        "type": "string",
                        "description": (
                            "支撑这个分数的具体证据：JD 原文哪一句、"
                            "简历/项目里哪一条。不许写“比较匹配”这类空话"
                        ),
                    },
                    "gap": {
                        "type": "string",
                        "description": "这一维缺什么，不缺就留空字符串",
                    },
                },
                "required": ["name", "score", "weight", "evidence", "gap"],
                "additionalProperties": False,
            },
        },
        "blockers": {
            "type": "array",
            "items": {"type": "string"},
            "description": "硬性阻断项（届别不符、学历门槛等），没有就空数组",
        },
        "quick_wins": {
            "type": "array",
            "items": {"type": "string"},
            "description": "今天就能做、能提高匹配度的具体动作",
        },
    },
    "required": [
        "company",
        "job_title",
        "job_url",
        "verdict",
        "dimensions",
        "blockers",
        "quick_wins",
    ],
    "additionalProperties": False,
}
