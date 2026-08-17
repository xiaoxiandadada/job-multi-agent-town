"""The controller identity, promoted from silent forwarder to a real agent.

Before this module the controller bot did two things: parse slash commands and
forward everything else. A plain message fell through to ``parse_run_command``'s
default branch, which carries no label, so the bot answered *nothing* until the
whole multi-role run finished — the user could not tell it from a dead bot.

The Chief of Staff keeps that same ``controller`` identity (no new registry
role, no eighth town building) and gives it four jobs the working roles cannot
do:

1. **Decomposition** — one model call that turns the request into at most three
   named subtasks. This replaces keyword routing, whose overlapping
   ``trigger_keywords`` woke six roles for a question that needed one.
2. **Dispatch receipt** — an instant reply saying what it read, who it picked,
   and why. Deterministic once the plan exists, so it costs no extra tokens.
3. **Attachment intake** — the one place pictures are turned into text
   evidence. With several roles selected, reading a screenshot once here is much
   cheaper than shipping the same image tokens to six roles; with a single role
   selected the raw picture goes straight to that role instead.
4. **Closing** — after the Evidence Judge has audited the evidence, one short
   "what to do next" pass, restricted to facts already on the page.

Only the two controller entry points (the Feishu controller bot and
``POST /api/runs``) go through here. The always-on patrol, the role bots, the
daily push and mock interviews keep calling the orchestrator directly, so none
of them pay for intake they do not need.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from collections.abc import Awaitable, Callable

from .models import (
    MAX_PLANNED_SUBTASKS,
    ImageAttachment,
    PlannedSubtask,
    RoleSpec,
    RunPlan,
    RunReport,
    RunRequest,
)
from .orchestrator import workflow_stage
from .structured import extract_json_object


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

PLANNER_ROLE = RoleSpec(
    role_id=CHIEF_ROLE_ID,
    display_name=CHIEF_DISPLAY_NAME,
    goal="把用户的一句话拆成最少的一组子任务并指派角色",
    system_prompt=(
        "你是 Chief of Staff 的任务拆解环节。你不回答用户的问题本身，"
        "只决定“派谁、做什么”。\n"
        "规则：\n"
        "1. 只输出一个 JSON 对象，不要任何解释文字，不要用代码块包裹。\n"
        f"2. subtasks 最多 {MAX_PLANNED_SUBTASKS} 个，能用 1 个角色解决就只派 1 个。"
        "宁少勿多——每多派一个角色，用户就多等一轮，并多收到一大段可能无关的内容。\n"
        "3. role_id 只能从候选清单里选，不得发明新角色，也不要派 judge。\n"
        "4. task 写这个角色在本次任务里具体要交付什么，"
        "不要复述角色的通用职责。\n"
        "5. depends_on 只在后一个角色确实需要前一个的产出时才填；"
        "能并行就留空。用户已经给出岗位或 JD 原文时，不要再派角色去找岗位。\n"
        "6. need_judge：多个角色的产出需要交叉核对，"
        "或涉及岗位真实性、届别、截止日期这类容易出错的事实时为 true；"
        "单个角色回答一个明确问题时为 false。\n"
        "7. skipped：一句话说明哪些有能力的角色被你排除了、为什么。\n"
        "输出格式：\n"
        '{"intent": "一句话复述用户要什么", "subtasks": [{"role_id": "...", '
        '"task": "...", "why": "...", "depends_on": []}], '
        '"need_judge": false, "skipped": "..."}'
    ),
    tools=[],
    model_profile="reliable",
    # Deciding who works this run is a routing judgement, not deep reasoning —
    # and it sits in front of everything else, so its latency is felt directly.
    effort="low",
    timeout_seconds=CHIEF_TIMEOUT_SECONDS,
)

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
    # Transcription, not analysis — the prompt above forbids interpreting. Depth
    # would buy nothing and this call sits in front of every role's work.
    effort="low",
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
    # Three lines quoting facts already on the page. This is the cheapest call in
    # the whole chain and the one with the least to think about.
    effort="low",
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


def planner_enabled(environ: dict[str, str] | None = None) -> bool:
    """Whether the decomposition call is on.

    Off falls back to ``trigger_keywords`` routing, which is what every direct
    caller (patrol, daily push) uses anyway. Kept switchable because it adds a
    call in front of a run that a latency-sensitive deployment may not want.
    """

    env = environ if environ is not None else os.environ
    return env.get("JOB_AGENT_CHIEF_PLANNER", "1").strip().casefold() not in {
        "0",
        "false",
        "off",
        "no",
    }


def planner_candidates(roles: list[RoleSpec]) -> str:
    """The role menu the planner picks from.

    Only id, name and goal — not the full ``system_prompt``. The planner decides
    *who*, and a 4000-character prompt per role would cost more than the run it
    is trying to shrink.
    """

    return "\n".join(
        f"- {role.role_id}（{role.display_name}）：{role.goal}"
        for role in roles
    )


def planner_prompt(query: str, roles: list[RoleSpec], *, image_count: int = 0) -> str:
    attachments = (
        f"\n用户还附了 {image_count} 张图片，正文会在下一步转写成文字证据。\n"
        if image_count
        else ""
    )
    return (
        f"用户请求：{query}\n"
        f"{attachments}\n"
        "可选角色：\n"
        f"{planner_candidates(roles)}\n\n"
        "请按系统提示词只输出拆解 JSON。"
    )


def parse_plan(text: str, allowed_role_ids: set[str]) -> RunPlan | None:
    """Turn a planner reply into a plan, or ``None`` to fall back.

    Every field is treated as untrusted: a hallucinated ``role_id`` would raise
    ``KeyError`` deep in the orchestrator, so unknown ids are dropped here. A
    plan left with no usable subtask is no plan at all — returning ``None`` sends
    the run to the keyword router instead of to zero roles.
    """

    payload = extract_json_object(text)
    if payload is None:
        return None
    raw_subtasks = payload.get("subtasks")
    if not isinstance(raw_subtasks, list):
        return None
    subtasks: list[PlannedSubtask] = []
    for item in raw_subtasks:
        if not isinstance(item, dict):
            continue
        role_id = str(item.get("role_id", "")).strip()
        if role_id not in allowed_role_ids:
            continue
        task = str(item.get("task", "")).strip()
        if len(task) < 2:
            # The task text becomes the task-graph node title; an empty one
            # would show up on the page as a blank card.
            task = "完成该角色在本次任务中的交付"
        raw_dependencies = item.get("depends_on")
        dependencies = (
            [
                str(value).strip()
                for value in raw_dependencies
                if str(value).strip() in allowed_role_ids
                and str(value).strip() != role_id
            ]
            if isinstance(raw_dependencies, list)
            else []
        )
        subtasks.append(
            PlannedSubtask(
                role_id=role_id,
                task=task[:600],
                why=str(item.get("why", "")).strip()[:300],
                depends_on=list(dict.fromkeys(dependencies)),
            )
        )
    if not subtasks:
        return None
    plan = RunPlan(
        intent=str(payload.get("intent", "")).strip()[:600],
        subtasks=subtasks,
        need_judge=bool(payload.get("need_judge", True)),
        skipped=str(payload.get("skipped", "")).strip()[:600],
        source="planner",
    )
    # ``cap_and_dedupe`` may have trimmed the list; a dependency on a role that
    # got cut would leave its task blocked forever in the graph.
    kept = set(plan.role_ids())
    return plan.model_copy(
        update={
            "subtasks": [
                subtask.model_copy(
                    update={
                        "depends_on": [
                            value
                            for value in subtask.depends_on
                            if value in kept
                        ]
                    }
                )
                for subtask in plan.subtasks
            ]
        }
    )


def keyword_plan(roles: list[RoleSpec], *, use_judge: bool = True) -> RunPlan:
    """The pre-planner routing, expressed as a plan.

    Lets the receipt, the task graph and the Judge decision read one shape no
    matter which router chose the roles.
    """

    return RunPlan(
        subtasks=[
            PlannedSubtask(role_id=role.role_id, task=role.goal)
            for role in roles
        ],
        need_judge=use_judge,
        source="keywords",
    )


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
    plan: RunPlan | None = None,
) -> str:
    """The receipt, sent before any specialist starts working.

    Deterministic given the plan: no second model call, no network. With a
    planner-authored plan it quotes the planner's own reasoning; without one it
    falls back to naming the keywords that matched, which is what the router
    actually used.
    """

    names = "、".join(role.display_name for role in roles) or "无可用角色"
    planned = plan if plan is not None and plan.source == "planner" else None
    by_id = {role.role_id: role for role in roles}
    if planned is not None:
        reasons = [
            f"{by_id[subtask.role_id].display_name}←{subtask.why}"
            for subtask in planned.subtasks
            if subtask.role_id in by_id and subtask.why
        ]
        basis = "、".join(reasons) if reasons else "已按任务拆解指派"
    elif requested_explicitly:
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
    intent = intent_line(planned.intent) if planned and planned.intent else intent_line(query)
    lines = [
        f"📋 {CHIEF_DISPLAY_NAME} 已接单",
        f"- 意图：{intent}",
        f"- 附件：{image_count} 张图片" if image_count else "- 附件：无",
        f"- 分派：{names}（{MODE_LABELS.get(mode, mode)}）",
        f"- {'拆解' if planned is not None else '依据'}：{basis}",
    ]
    if planned is not None and planned.skipped:
        lines.append(f"- 未派：{intent_line(planned.skipped, limit=80)}")
    lines.append(
        f"- 收尾：{' → '.join(closers)}" if closers else "- 收尾：直接返回角色原文"
    )
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

    def __init__(
        self,
        orchestrator,
        *,
        with_closing: bool | None = None,
        with_planner: bool | None = None,
    ):
        self.orchestrator = orchestrator
        self.with_closing = (
            closing_enabled() if with_closing is None else with_closing
        )
        self.with_planner = (
            planner_enabled() if with_planner is None else with_planner
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

    async def _plan(
        self,
        base,
        request: RunRequest,
        *,
        run_id: str,
    ) -> tuple[RunPlan | None, int, int]:
        """One decomposition call. Returns (plan, input_tokens, output_tokens).

        Never raises: a planner that times out or answers with prose must cost
        the run nothing but the attempt. ``None`` means "use the keyword
        router", and the reason is on the timeline either way.
        """

        candidates = [
            role
            for role in base.registry.list_roles()
            if workflow_stage(role) != "judge"
        ]
        if not candidates:
            return None, 0, 0
        started = time.perf_counter()
        try:
            reply = await asyncio.wait_for(
                base.model_client.complete(
                    PLANNER_ROLE,
                    planner_prompt(
                        request.query,
                        candidates,
                        image_count=len(request.images),
                    ),
                ),
                timeout=PLANNER_ROLE.timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001 - degrade, never fail the run
            base._record(
                run_id=run_id,
                kind="plan_created",
                status="error",
                phase="intake",
                mode=request.mode,
                role_id=CHIEF_ROLE_ID,
                display_name=CHIEF_DISPLAY_NAME,
                latency_ms=(time.perf_counter() - started) * 1000,
                error=f"{exc}；改用关键词路由",
            )
            return None, 0, 0
        plan = parse_plan(
            reply.content,
            {role.role_id for role in candidates},
        )
        if plan is None:
            base._record(
                run_id=run_id,
                kind="plan_created",
                status="error",
                phase="intake",
                mode=request.mode,
                role_id=CHIEF_ROLE_ID,
                display_name=CHIEF_DISPLAY_NAME,
                model=reply.model,
                latency_ms=(time.perf_counter() - started) * 1000,
                output=reply.content,
                error="拆解结果无法解析为可用任务，改用关键词路由",
                metrics={
                    "input_tokens": reply.input_tokens,
                    "output_tokens": reply.output_tokens,
                },
            )
            return None, reply.input_tokens, reply.output_tokens
        base._record(
            run_id=run_id,
            kind="plan_created",
            status="ok",
            phase="intake",
            mode=request.mode,
            role_id=CHIEF_ROLE_ID,
            display_name=CHIEF_DISPLAY_NAME,
            model=reply.model,
            latency_ms=(time.perf_counter() - started) * 1000,
            query=request.query,
            output="\n".join(
                f"{subtask.role_id}：{subtask.task}"
                for subtask in plan.subtasks
            ),
            selected_role_ids=plan.role_ids(),
            target_role_ids=plan.role_ids(),
            metrics={
                "subtask_count": len(plan.subtasks),
                "need_judge": plan.need_judge,
                "input_tokens": reply.input_tokens,
                "output_tokens": reply.output_tokens,
            },
        )
        return plan, reply.input_tokens, reply.output_tokens

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
        chief_input = 0
        chief_output = 0
        chief_calls = 0

        # Decompose first: who works on this run is the planner's call, and the
        # receipt below should report that decision rather than a keyword table.
        # An explicit role request (a Feishu role bot, ``/ask jd_analyst``) has
        # already answered the question, so it skips the call entirely.
        plan: RunPlan | None = request.plan
        if plan is None and self.with_planner and not request.requested_roles:
            plan, used_in, used_out = await self._plan(
                base,
                request,
                run_id=run_id,
            )
            if used_in or used_out:
                # A rejected plan still burned tokens. Count them, or the
                # metrics under-report what the run cost.
                chief_calls += 1
                chief_input += used_in
                chief_output += used_out

        inner = request if plan is None else request.model_copy(
            update={"plan": plan}
        )
        if (
            plan is not None
            and request.mode == "parallel"
            and any(subtask.depends_on for subtask in plan.subtasks)
        ):
            # The planner said one role should build on another's evidence, and
            # ``collaborative`` is the only mode that actually hands it over
            # (see the handoff blocks in ``MultiAgentOrchestrator.run``). Left as
            # ``parallel`` the two would run side by side and the dependency
            # would be a note nobody acted on.
            inner = inner.model_copy(update={"mode": "collaborative"})
        selected = base.select_roles(inner)
        if plan is None:
            # Keep one shape downstream: the task graph and the Judge decision
            # read a plan whether or not the planner produced it.
            plan = keyword_plan(selected, use_judge=request.use_judge)
            inner = request.model_copy(update={"plan": plan})

        note = dispatch_note(
            request.query,
            selected,
            mode=inner.mode,
            image_count=len(request.images),
            requested_explicitly=bool(request.requested_roles),
            use_judge=request.use_judge and plan.need_judge,
            with_closing=self.with_closing,
            plan=plan,
        )
        base._record(
            run_id=run_id,
            kind="intake_completed",
            status="ok",
            phase="intake",
            mode=inner.mode,
            role_id=CHIEF_ROLE_ID,
            display_name=CHIEF_DISPLAY_NAME,
            query=request.query,
            output=note,
            selected_role_ids=[role.role_id for role in selected],
            target_role_ids=[role.role_id for role in selected],
            metrics={
                "image_count": len(request.images),
                "plan_source": plan.source,
            },
        )
        if notify is not None:
            await notify(note)

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
                    # Copy from ``inner``, not ``request``: the plan was already
                    # attached above and rebuilding from the original request
                    # would silently drop it, sending the run back to keyword
                    # routing whenever the user attached a screenshot.
                    inner = inner.model_copy(
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
