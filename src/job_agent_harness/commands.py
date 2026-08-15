from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal


RunMode = Literal["single", "sequential", "parallel", "collaborative"]


@dataclass(frozen=True)
class RunCommand:
    query: str
    requested_roles: list[str]
    mode: RunMode
    label: str | None = None
    #: Per-run override of every selected role's own timeout. A standing flow
    #: needs it: in ``collaborative`` mode an action-stage role receives every
    #: upstream role's full output, so its input grows with the run while its
    #: timeout would otherwise stay at the 150-180 s tuned for a single question.
    timeout_seconds: float | None = None


#: What a standing flow gets instead of a role's own timeout. Matched to the
#: always-on patrol's budget, which faces the same problem: nobody is watching
#: the clock, and the work legitimately takes minutes.
FLOW_TIMEOUT_SECONDS = 300.0


#: The standing morning flow, written out so ``/today`` needs no argument. This
#: is the whole point of the command: the daily routine is the same every day,
#: and making the user retype it is the friction that stops it being used. The
#: text names what each stage owes the next, because ``collaborative`` mode hands
#: each phase's output to the following one as evidence.
DAILY_FLOW_TASK = (
    "跑一遍今天的求职流程，四步都要落到具体岗位上：\n"
    "1. 核验今天新增或仍然开放的 2027 届正式校招岗位，逐个给出公司、岗位全名、"
    "完整官方链接、网申截止时间，并排出投递优先级；查不到就写“查不到”，不要凑数量。\n"
    "2. 取优先级最高的那个岗位，拆解硬要求、加分项、ATS 关键词和我的技能缺口，"
    "并把缺口涉及的核心概念讲到我今天能面试、能动手的深度。\n"
    "3. 针对这个岗位给出今天做得完的简历改写动作（改哪几条 bullet、改写前后）"
    "和一项今天能推进、能验证的作品动作。\n"
    "4. 出今天的模拟面试题：一道项目深挖题、一道技术追问题，各写追问链条和口述要点。\n"
    "全程只用有出处的事实，今天做不完的不要写进来。"
)

#: ``(label, role_ids, mode, default_task)``. ``default_task`` of ``None`` means
#: the command requires an argument; a string makes the argument optional and is
#: used as the task when none is given.
PRESET_COMMANDS: dict[str, tuple[str, list[str], RunMode, str | None]] = {
    "/knowledge": (
        "岗位知识补充",
        ["job_analyst"],
        "single",
        None,
    ),
    "/job": (
        "岗位准备协作",
        ["job_scout", "job_analyst"],
        "collaborative",
        None,
    ),
    "/apply": (
        "投递材料协作",
        ["job_analyst", "material_builder"],
        "collaborative",
        None,
    ),
    "/interview": (
        "面试准备协作",
        ["job_analyst", "interview_coach"],
        "collaborative",
        None,
    ),
    "/match": (
        "岗位匹配度评分",
        ["job_analyst", "match_scorer"],
        "collaborative",
        None,
    ),
    "/team": (
        "全角色协作",
        [
            "job_scout",
            "job_analyst",
            "material_builder",
            "interview_coach",
        ],
        "collaborative",
        None,
    ),
    "/today": (
        "今日求职流程",
        [
            "job_scout",
            "job_analyst",
            "match_scorer",
            "material_builder",
            "interview_coach",
        ],
        "collaborative",
        DAILY_FLOW_TASK,
    ),
}

#: Compound phrases that mean "run the standing flow". Each already contains
#: 流程, so no separate day check is needed — and a bare 流程 is deliberately not
#: here: "这个岗位的面试流程是几轮" must keep going to the planner.
_FLOW_TASK_WORDS = (
    "求职流程",
    "全流程",
    "整套流程",
    "例行流程",
    "今日流程",
    "今天的流程",
)


def looks_like_daily_flow(text: str) -> bool:
    """Whether a plain sentence is asking for the standing daily flow.

    Exists because the flow needs four roles and the planner caps itself at
    three — so a sentence like "做一下今日的求职流程" cannot reach the full flow
    through decomposition, no matter how well the planner reads it.
    """

    value = re.sub(r"\s+", "", text)
    return any(word in value for word in _FLOW_TASK_WORDS)


HELP_TEXT = """# Chief of Staff 命令

我是这支求职团队的调度台：读你的图和文字 → 把任务拆成最少的几件事 → 说明我选了谁、
为什么 → 让角色干活 → 需要时由 Evidence Judge 审证据 → 我给一句下一步。

**群里必须 `@Chief of Staff`。** 飞书只在机器人被点名时才把群消息推给它，
`@所有人` 属于 at_all，点不到机器人，所以那条消息我根本收不到——不是我没反应。

## 一句话跑完整套流程

- `@Chief of Staff /today`：今日求职流程，**不用带参数**。四个角色分阶段协作：
  核验今日岗位 → 拆解要求与缺口 → 打匹配度分 → 出简历与作品动作 → 出模拟面试题，
  最后由 Evidence Judge 核对来源。一次约 7 次模型调用，请给它几分钟。
- `@Chief of Staff /today 重点看字节`：同一套流程，额外加一条你的侧重。
- 直接说「做一下今日的求职流程」也会走 `/today`——同时提到「今天」和「流程」就够。

## 其他命令

- 直接发图片：我先读成文字证据（公司/岗位/日期/技能一律照抄，看不清写“图中不可辨认”），
  再接着说要我做什么即可；一次最多 9 张，暂存 3 分钟
- `/help`：查看帮助，不调用模型
- `/roles`：查看角色，不调用模型
- `/daily`：推送今天的结构化日报
- `/daily YYYY-MM-DD`：推送指定日期日报
- `/daily full`：推送今天的完整日报
- `/group-create`：由总控创建并绑定私有 Agent 小镇群
- `/knowledge <任务>`：Job Analyst 单独拆解岗位要求与所需知识
- `/job <任务>`：岗位侦察 + 岗位分析
- `/apply <任务>`：岗位分析先行，再产出简历与作品材料
- `/interview <任务>`：岗位分析先行，再生成面试准备
- `/match <岗位或 JD>`：只算匹配度——五维评分 + 证据 + 今天能提分的动作
- `/team <任务>`：4 个工作角色分阶段协作（你给任务；不给任务用 `/today`）
- `/mock start <主题> [语音] [N轮]`：Interview Coach 一问一答的实时模拟面试
- `/mock skip` / `/mock status` / `/mock end`：跳过本题 / 查看进度 / 结束复盘
- `/ask <role_id> <问题>`：向指定角色提问（跳过任务拆解）
- `/agent <role_id> <任务>`：`/ask` 的兼容别名
- `/role-add <JSON>`：新增并立即启用角色

只发一个 `/` 我就把上面这张表展开发给你，不用记。

其他普通消息由我先拆解成最多 3 个子任务再派单，所以一句话通常只唤起一到两个角色。
我会先回一条「意图 / 分派 / 拆解依据」的接单说明，再交给角色执行，最后补一段下一步。"""


def parse_run_command(text: str) -> RunCommand:
    value = text.strip()
    if value.startswith(("/agent", "/ask")):
        parts = value.split(maxsplit=2)
        if len(parts) != 3:
            raise ValueError("用法：/ask <role_id> <问题>")
        return RunCommand(
            query=parts[2],
            requested_roles=[parts[1]],
            mode="single",
            label=f"指定角色 {parts[1]}",
        )

    command, separator, query = value.partition(" ")
    if command in PRESET_COMMANDS:
        label, role_ids, mode, default_task = PRESET_COMMANDS[command]
        extra = query.strip() if separator else ""
        if default_task is None:
            if not extra:
                raise ValueError(f"用法：{command} <任务>")
            task = extra
        elif extra:
            # Keep the standing flow and append the steer, rather than replacing
            # it — "/today 重点看字节" should still run all four stages.
            task = f"{default_task}\n\n用户补充要求：{extra}"
        else:
            task = default_task
        return RunCommand(
            query=task,
            requested_roles=role_ids,
            mode=mode,
            label=label,
            timeout_seconds=(
                FLOW_TIMEOUT_SECONDS if default_task is not None else None
            ),
        )

    if value.startswith("/"):
        raise ValueError("未知命令。发送 /help 查看可用命令。")

    if looks_like_daily_flow(value):
        label, role_ids, mode, default_task = PRESET_COMMANDS["/today"]
        return RunCommand(
            query=f"{default_task}\n\n用户原话：{value}",
            requested_roles=role_ids,
            mode=mode,
            label=label,
            timeout_seconds=FLOW_TIMEOUT_SECONDS,
        )

    return RunCommand(
        query=value,
        requested_roles=[],
        mode="parallel",
    )


@dataclass(frozen=True)
class CommandInfo:
    """One command, as the UI needs to show it."""

    name: str
    #: Argument placeholder, e.g. ``<任务>``. Empty means the command works bare.
    hint: str
    summary: str
    #: Which roles it wakes. Empty for commands that call no model.
    roles: list[str]


#: Single source of truth for the command palette. The web input reads this over
#: HTTP rather than shipping its own copy: a hardcoded list in JavaScript drifts
#: from ``PRESET_COMMANDS`` the first time anyone adds a command, and then the
#: palette confidently offers something the parser rejects.
_EXTRA_COMMAND_SUMMARIES: dict[str, tuple[str, str]] = {
    "/help": ("", "查看全部命令，不调用模型"),
    "/roles": ("", "查看当前角色与模型，不调用模型"),
    "/daily": ("[日期] [full]", "推送结构化日报，可加日期或 full"),
    "/ask": ("<role_id> <问题>", "只问一个角色，跳过任务拆解"),
    "/agent": ("<role_id> <任务>", "/ask 的兼容别名"),
    "/mock": ("start <主题>", "进入一问一答的实时模拟面试"),
    "/role-add": ("<JSON>", "新增并立即启用一个角色"),
    "/group-create": ("", "创建并绑定私有 Agent 小镇群"),
}

_PRESET_SUMMARIES: dict[str, str] = {
    "/today": "今日求职流程，不用带参数——五个角色跑完整套",
    "/match": "只算匹配度：五维评分 + 证据 + 今天能提分的动作",
    "/job": "岗位侦察 + 岗位分析",
    "/apply": "岗位分析先行，再产出简历与作品材料",
    "/interview": "岗位分析先行，再生成面试准备",
    "/knowledge": "单独拆解岗位要求与所需知识",
    "/team": "全部工作角色分阶段协作（你给任务）",
}


def command_catalog() -> list[CommandInfo]:
    """Every command the palette should offer, standing flows first.

    Ordered by how often a user reaches for it rather than alphabetically: the
    whole point of the palette is that the useful thing is the first thing.
    """

    catalog: list[CommandInfo] = []
    for name in ("/today", "/match", "/job", "/apply", "/interview", "/knowledge", "/team"):
        if name not in PRESET_COMMANDS:
            continue
        _, roles, _, default_task = PRESET_COMMANDS[name]
        catalog.append(
            CommandInfo(
                name=name,
                hint="" if default_task is not None else "<任务>",
                summary=_PRESET_SUMMARIES.get(name, PRESET_COMMANDS[name][0]),
                roles=list(roles),
            )
        )
    for name, (hint, summary) in _EXTRA_COMMAND_SUMMARIES.items():
        catalog.append(CommandInfo(name=name, hint=hint, summary=summary, roles=[]))
    return catalog


#: What a bare ``/`` is answered with. Feishu's own slash panel is configured in
#: the developer console and cannot be created from here, so the next best thing
#: is to treat ``/`` as a command in its own right and expand the catalog inline.
PALETTE_TRIGGER = "/"


def command_palette_markdown() -> str:
    """The catalog as a panel, for a channel that has no palette widget.

    Built from ``command_catalog`` rather than written out, for the same reason
    the web input reads that function over HTTP: a second hand-maintained list
    starts agreeing with ``PRESET_COMMANDS`` and stops the first time someone
    adds a command. The role count is shown because it is the only hint the user
    gets about what a command will cost in time — ``/today`` waking five roles is
    a different decision from ``/knowledge`` waking one.
    """

    catalog = command_catalog()
    lines = ["# 可用命令", "", "**求职流程**（会调用模型，角色数越多越慢）"]
    for info in catalog:
        if not info.roles:
            continue
        usage = f"{info.name} {info.hint}".strip()
        lines.append(f"- `{usage}` · {len(info.roles)} 个角色 —— {info.summary}")
    lines.append("")
    lines.append("**工具命令**")
    for info in catalog:
        if info.roles:
            continue
        usage = f"{info.name} {info.hint}".strip()
        lines.append(f"- `{usage}` —— {info.summary}")
    lines.append("")
    lines.append(
        "不带命令直接说人话也行，我会自己判断该派谁。"
        "只想问一个知识点就用 `/ask job_analyst <问题>`，最快。"
    )
    return "\n".join(lines)
