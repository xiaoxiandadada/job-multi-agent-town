from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


RunMode = Literal["single", "sequential", "parallel", "collaborative"]


@dataclass(frozen=True)
class RunCommand:
    query: str
    requested_roles: list[str]
    mode: RunMode
    label: str | None = None


PRESET_COMMANDS: dict[str, tuple[str, list[str], RunMode]] = {
    "/knowledge": (
        "岗位知识补充",
        ["job_knowledge_curator"],
        "single",
    ),
    "/job": (
        "岗位准备协作",
        ["job_scout", "jd_analyst", "job_knowledge_curator"],
        "collaborative",
    ),
    "/apply": (
        "投递材料协作",
        [
            "jd_analyst",
            "job_knowledge_curator",
            "resume_strategist",
            "portfolio_coach",
        ],
        "collaborative",
    ),
    "/interview": (
        "面试准备协作",
        ["jd_analyst", "job_knowledge_curator", "interview_coach"],
        "collaborative",
    ),
    "/team": (
        "全角色协作",
        [
            "job_scout",
            "jd_analyst",
            "job_knowledge_curator",
            "resume_strategist",
            "portfolio_coach",
            "interview_coach",
        ],
        "collaborative",
    ),
}


HELP_TEXT = """# Chief of Staff 命令

我是这支求职团队的调度台：读你的图和文字 → 说明我选了谁、为什么 → 让角色干活 →
Evidence Judge 审证据 → 我给一句下一步。

- 直接发图片：我先读成文字证据（公司/岗位/日期/技能一律照抄，看不清写“图中不可辨认”），
  再接着说要我做什么即可；一次最多 9 张，暂存 3 分钟
- `/help`：查看帮助，不调用模型
- `/roles`：查看角色，不调用模型
- `/daily`：推送今天的结构化日报
- `/daily YYYY-MM-DD`：推送指定日期日报
- `/daily full`：推送今天的完整日报
- `/group-create`：由总控创建并绑定 8-Bot 私有 Agent 小镇群
- `/knowledge <任务>`：Knowledge Curator + Judge
- `/job <任务>`：岗位侦察 + JD 分析 + 岗位知识 + Judge
- `/apply <任务>`：JD/知识先行，再生成简历与作品建议
- `/interview <任务>`：JD/知识先行，再生成面试准备
- `/mock start <主题> [语音] [N轮]`：Interview Coach 一问一答的实时模拟面试
- `/mock skip` / `/mock status` / `/mock end`：跳过本题 / 查看进度 / 结束复盘
- `/team <任务>`：6 个工作角色分两阶段协作 + Judge
- `/ask <role_id> <问题>`：向指定角色提问
- `/agent <role_id> <任务>`：`/ask` 的兼容别名
- `/role-add <JSON>`：新增并立即启用角色

普通消息会按关键词自动选择相关角色；我会先回一条「意图 / 分派 / 依据」的接单说明，
再由 Judge 汇总，最后我补一段下一步。"""


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
        if not separator or not query.strip():
            raise ValueError(f"用法：{command} <任务>")
        label, role_ids, mode = PRESET_COMMANDS[command]
        return RunCommand(
            query=query.strip(),
            requested_roles=role_ids,
            mode=mode,
            label=label,
        )

    if value.startswith("/"):
        raise ValueError("未知命令。发送 /help 查看可用命令。")

    return RunCommand(
        query=value,
        requested_roles=[],
        mode="parallel",
    )
