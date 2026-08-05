"""The controller identity, promoted from silent forwarder to a real agent.

Before this module the controller bot did two things: parse slash commands and
forward everything else. A plain message fell through to ``parse_run_command``'s
default branch, which carries no label, so the bot answered *nothing* until the
whole multi-role run finished — the user could not tell it from a dead bot.

The Chief of Staff keeps that same ``controller`` identity (no new registry
role, no eighth town building) and gives it three jobs the working roles cannot
do:

1. **Dispatch receipt** — an instant, model-free reply saying what it read, who
   it picked, and why. Zero latency, zero tokens, so it costs nothing to always
   send it.
2. **Attachment intake** — the one place pictures are turned into text
   evidence. With several roles selected, reading a screenshot once here is much
   cheaper than shipping the same image tokens to six roles; with a single role
   selected the raw picture goes straight to that role instead.
3. **Closing** — after the Evidence Judge has audited the evidence, one short
   "what to do next" pass, restricted to facts already on the page.

Only the two controller entry points (the Feishu controller bot and
``POST /api/runs``) go through here. The always-on patrol, the role bots, the
daily push and mock interviews keep calling the orchestrator directly, so none
of them pay for intake they do not need.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections.abc import Awaitable, Callable

from .models import ImageAttachment, RoleSpec, RunReport, RunRequest


CHIEF_ROLE_ID = "controller"
CHIEF_DISPLAY_NAME = "Chief of Staff"

#: How the receipt names each mode, so the user can tell a fan-out from a
#: single-role answer without knowing the codebase.
MODE_LABELS = {
    "single": "单角色",
    "sequential": "串行",
    "parallel": "并行",
    "collaborative": "分阶段协作",
}

#: Reading pictures and writing a closing line are cheap next to a role's tool
#: loop, but they sit in front of / behind it, so they get their own short
#: budget instead of the 150-180 s a working role is allowed.
CHIEF_TIMEOUT_SECONDS = 90.0

VISION_ROLE = RoleSpec(
    role_id=CHIEF_ROLE_ID,
    display_name=CHIEF_DISPLAY_NAME,
    goal="把用户附件转成下游 Agent 可用的文字证据",
    system_prompt=(
        "你是 Chief of Staff 的读图环节，只做转写，不做分析。"
        "把图片里真实可见的内容抄成文字，供下游求职 Agent 当证据使用。\n"
        "规则：\n"
        "1. 只写图中确实能看清的信息，公司名、岗位全名、届别与学历要求、"
        "技能项、日期、薪资、链接一律照抄原文，不要改写措辞。\n"
        "2. 看不清、被截断、被遮挡的内容写“图中不可辨认”；"
        "绝对不要猜公司、不要补全链接、不要用常识推断岗位或截止时间。\n"
        "3. 不给建议、不做匹配分析、不总结成抽象条目——那是下游角色的工作。\n"
        "4. 固定输出三段：一、图片类型（JD 截图／简历／网页／聊天／其他）；"
        "二、逐条原文事实；三、图中不可辨认的部分。"
    ),
    tools=[],
    model_profile="reliable",
    timeout_seconds=CHIEF_TIMEOUT_SECONDS,
)

CLOSING_ROLE = RoleSpec(
    role_id=CHIEF_ROLE_ID,
    display_name=CHIEF_DISPLAY_NAME,
    goal="在 Evidence Judge 审完之后给出下一步该做的一件事",
    system_prompt=(
        "你是 Chief of Staff 的收口环节。Evidence Judge 已经审过证据，"
        "你只负责补一段“下一步”。\n"
        "规则：\n"
        "1. 只能引用上文已经出现的事实，不得引入新的岗位、链接、日期或指标；"
        "上文没有的就不要写。\n"
        "2. 不复述、不总结上文内容。\n"
        "3. 最多 200 字，固定三行：下一步先做的一件事；为什么是它；"
        "完成判据（做完能看到的具体产物）。"
    ),
    tools=[],
    model_profile="reliable",
    timeout_seconds=CHIEF_TIMEOUT_SECONDS,
)

#: Heading the closing is appended under, so the Feishu message and the web
#: chat window both show it as the chief speaking rather than as more Judge.
CLOSING_HEADING = f"## {CHIEF_DISPLAY_NAME} · 下一步"


def closing_enabled(environ: dict[str, str] | None = None) -> bool:
    """Whether the closing model call is on.

    It is one extra call at the very end of an already slow run, so a
    latency-sensitive deployment can drop it and keep the receipt and the
    picture reading.
    """

    env = environ if environ is not None else os.environ
    return env.get("JOB_AGENT_CHIEF_CLOSING", "1").strip().casefold() not in {
        "0",
        "false",
        "off",
        "no",
    }


def matched_keywords(role: RoleSpec, query: str) -> list[str]:
    """The trigger words that put this role on the list.

    Mirrors ``MultiAgentOrchestrator.select_roles`` exactly — same casefolded
    substring test — so the receipt explains the real routing decision instead
    of a plausible-looking guess about it.
    """

    lowered = query.casefold()
    return [
        keyword
        for keyword in role.trigger_keywords
        if keyword.casefold() in lowered
    ]


def intent_line(query: str, limit: int = 60) -> str:
    normalized = " ".join(query.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1] + "…"


def dispatch_note(
    query: str,
    roles: list[RoleSpec],
    *,
    mode: str,
    image_count: int = 0,
    requested_explicitly: bool = False,
    use_judge: bool = True,
    with_closing: bool = True,
) -> str:
    """The instant receipt. Deterministic, no model call, no network.

    This is the fix for the original complaint: whatever else happens, the user
    learns within a second what the controller understood and who it woke up.
    """

    names = "、".join(role.display_name for role in roles) or "无可用角色"
    if requested_explicitly:
        basis = "用户直接指定角色"
    else:
        hits = [
            f"{role.display_name}←{'/'.join(matched_keywords(role, query))}"
            for role in roles
            if matched_keywords(role, query)
        ]
        basis = "、".join(hits) if hits else "无关键词命中，按默认角色兜底"
    closers = []
    if use_judge:
        closers.append("Evidence Judge 审证据")
    if with_closing:
        closers.append(f"{CHIEF_DISPLAY_NAME} 给下一步")
    lines = [
        f"📋 {CHIEF_DISPLAY_NAME} 已接单",
        f"- 意图：{intent_line(query)}",
        f"- 附件：{image_count} 张图片" if image_count else "- 附件：无",
        f"- 分派：{names}（{MODE_LABELS.get(mode, mode)}）",
        f"- 依据：{basis}",
        f"- 收尾：{' → '.join(closers)}" if closers else "- 收尾：直接返回角色原文",
    ]
    return "\n".join(lines)


def attachment_prompt(images: list[ImageAttachment]) -> str:
    names = "、".join(
        image.source_name for image in images if image.source_name
    )
    source = f"（文件名：{names}）" if names else ""
    return (
        f"用户发来 {len(images)} 张图片{source}。"
        "请按系统提示词把图中可见内容转写成文字证据。"
    )


def digest_block(digest: str) -> str:
    return (
        f"## 用户附件（{CHIEF_DISPLAY_NAME} 读图转写，非模型推断）\n\n{digest}"
    )


def closing_prompt(query: str, final_output: str) -> str:
    return (
        f"用户任务：{query}\n\n"
        "以下是本轮已经交付并审核过的内容：\n\n"
        f"{final_output}\n\n"
        "请按系统提示词只输出“下一步”三行。"
    )


class ChiefOfStaff:
    """Wraps one orchestrator run with intake and closing.

    It deliberately takes the orchestrator rather than being one: the run
    contract (``run(request) -> RunReport``) stays exactly as every other caller
    knows it, and both orchestrator implementations work here unchanged.
    """

    def __init__(self, orchestrator, *, with_closing: bool | None = None):
        self.orchestrator = orchestrator
        self.with_closing = (
            closing_enabled() if with_closing is None else with_closing
        )

    async def _base(self):
        """The plain ``MultiAgentOrchestrator`` under whatever we were handed.

        ``LazyOrchestrator`` builds the model stack on first use, so ask it to
        resolve rather than reaching for a ``base`` that does not exist yet.
        """

        orchestrator = self.orchestrator
        resolve = getattr(orchestrator, "resolve", None)
        if resolve is not None:
            orchestrator = await resolve()
        return getattr(orchestrator, "base", orchestrator)

    async def _read_attachments(
        self,
        base,
        request: RunRequest,
        *,
        run_id: str,
    ) -> tuple[str, int, int]:
        """One vision call. Returns (digest, input_tokens, output_tokens)."""

        started = time.perf_counter()
        reply = await asyncio.wait_for(
            base.model_client.complete(
                VISION_ROLE,
                attachment_prompt(request.images),
                images=request.images,
            ),
            timeout=VISION_ROLE.timeout_seconds,
        )
        base._record(
            run_id=run_id,
            kind="attachment_read",
            status="ok",
            phase="intake",
            mode=request.mode,
            role_id=CHIEF_ROLE_ID,
            display_name=CHIEF_DISPLAY_NAME,
            model=reply.model,
            latency_ms=(time.perf_counter() - started) * 1000,
            output=reply.content,
            metrics={
                "image_count": len(request.images),
                "input_tokens": reply.input_tokens,
                "output_tokens": reply.output_tokens,
            },
        )
        return reply.content, reply.input_tokens, reply.output_tokens

    async def _closing(
        self,
        base,
        request: RunRequest,
        report: RunReport,
        *,
        run_id: str,
    ) -> tuple[str, int, int]:
        started = time.perf_counter()
        reply = await asyncio.wait_for(
            base.model_client.complete(
                CLOSING_ROLE,
                closing_prompt(request.query, report.final_output),
            ),
            timeout=CLOSING_ROLE.timeout_seconds,
        )
        base._record(
            run_id=run_id,
            kind="closing_created",
            status="ok",
            phase="delivery",
            mode=request.mode,
            role_id=CHIEF_ROLE_ID,
            display_name=CHIEF_DISPLAY_NAME,
            model=reply.model,
            latency_ms=(time.perf_counter() - started) * 1000,
            output=reply.content,
            metrics={
                "input_tokens": reply.input_tokens,
                "output_tokens": reply.output_tokens,
            },
        )
        return reply.content, reply.input_tokens, reply.output_tokens

    async def run(
        self,
        request: RunRequest,
        *,
        notify: Callable[[str], Awaitable[None]] | None = None,
    ) -> RunReport:
        run_id = str(uuid.uuid4())
        base = await self._base()
        selected = base.select_roles(request)

        note = dispatch_note(
            request.query,
            selected,
            mode=request.mode,
            image_count=len(request.images),
            requested_explicitly=bool(request.requested_roles),
            use_judge=request.use_judge,
            with_closing=self.with_closing,
        )
        base._record(
            run_id=run_id,
            kind="intake_completed",
            status="ok",
            phase="intake",
            mode=request.mode,
            role_id=CHIEF_ROLE_ID,
            display_name=CHIEF_DISPLAY_NAME,
            query=request.query,
            output=note,
            selected_role_ids=[role.role_id for role in selected],
            target_role_ids=[role.role_id for role in selected],
            metrics={"image_count": len(request.images)},
        )
        if notify is not None:
            await notify(note)

        inner = request
        chief_input = 0
        chief_output = 0
        chief_calls = 0
        if request.images:
            if len(selected) == 1:
                # One role, one look. Handing over the raw picture beats a
                # transcription of it, and there is nobody to share the cost
                # with anyway.
                base._record(
                    run_id=run_id,
                    kind="attachment_read",
                    status="ok",
                    phase="intake",
                    mode=request.mode,
                    role_id=CHIEF_ROLE_ID,
                    display_name=CHIEF_DISPLAY_NAME,
                    output=(
                        f"{len(request.images)} 张图片直接交给 "
                        f"{selected[0].display_name} 自己看，未做转写"
                    ),
                    target_role_ids=[selected[0].role_id],
                    metrics={
                        "image_count": len(request.images),
                        "transcribed": False,
                    },
                )
            else:
                try:
                    digest, used_in, used_out = await self._read_attachments(
                        base,
                        request,
                        run_id=run_id,
                    )
                except Exception as exc:  # noqa: BLE001 - fail soft, see below
                    # A failed transcription must not cost the user the run:
                    # keep the raw pictures on the request so the roles can
                    # still look, and say so instead of silently dropping them.
                    base._record(
                        run_id=run_id,
                        kind="attachment_read",
                        status="error",
                        phase="intake",
                        mode=request.mode,
                        role_id=CHIEF_ROLE_ID,
                        display_name=CHIEF_DISPLAY_NAME,
                        error=str(exc),
                        metrics={"image_count": len(request.images)},
                    )
                    if notify is not None:
                        await notify(
                            f"⚠️ {CHIEF_DISPLAY_NAME} 读图失败："
                            f"{exc}。图片改为直接交给各角色自己看。"
                        )
                else:
                    chief_calls += 1
                    chief_input += used_in
                    chief_output += used_out
                    inner = request.model_copy(
                        update={
                            "query": (
                                f"{request.query}\n\n{digest_block(digest)}"
                            ),
                            # Read once, spent once. Six roles re-reading the
                            # same screenshot is the cost this line removes.
                            "images": [],
                        }
                    )

        report = await self.orchestrator.run(inner, run_id=run_id)

        final_output = report.final_output
        if self.with_closing and final_output.strip():
            try:
                closing, used_in, used_out = await self._closing(
                    base,
                    request,
                    report,
                    run_id=run_id,
                )
            except Exception as exc:  # noqa: BLE001 - the run itself is fine
                base._record(
                    run_id=run_id,
                    kind="closing_created",
                    status="error",
                    phase="delivery",
                    mode=request.mode,
                    role_id=CHIEF_ROLE_ID,
                    display_name=CHIEF_DISPLAY_NAME,
                    error=str(exc),
                )
            else:
                chief_calls += 1
                chief_input += used_in
                chief_output += used_out
                final_output = (
                    f"{final_output}\n\n---\n\n{CLOSING_HEADING}\n\n{closing}"
                )

        if not chief_calls and final_output == report.final_output:
            return report
        # Count the chief's own calls and tokens. Latency fields keep measuring
        # the agent phase only — the two chief calls report their own latency on
        # their own activity events.
        metrics = report.metrics.model_copy(
            update={
                "model_calls": report.metrics.model_calls + chief_calls,
                "input_tokens": report.metrics.input_tokens + chief_input,
                "output_tokens": report.metrics.output_tokens + chief_output,
            }
        )
        return report.model_copy(
            update={"final_output": final_output, "metrics": metrics}
        )
