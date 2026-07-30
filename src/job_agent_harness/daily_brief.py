from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo


DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SECTION_PATTERN = re.compile(r"^##\s+(.+?)\s*$")
LEARNING_SECTIONS = {
    "今日 AI / Agent 学习任务": "AI / Agent",
    "今日 SQL 题": "SQL",
    "今日 LeetCode 题": "LeetCode",
    "今日机器学习面试题": "机器学习",
    "今日数学/概率统计题": "概率统计",
}
ROLE_DAILY_LABELS = {
    "job_scout": ("岗位侦察员", "新增岗位、链接核验与投递优先级"),
    "jd_analyst": ("JD 分析师", "岗位要求、匹配点与技能缺口"),
    "job_knowledge_curator": (
        "岗位知识补充员",
        "书籍章节、技术栈与学习产出",
    ),
    "resume_strategist": ("简历策略师", "优先简历版本与 bullet 动作"),
    "portfolio_coach": ("作品教练", "Vibe Coding 灵感与作品推进"),
    "interview_coach": ("面试教练", "具体题目、口述与追问训练"),
    "judge": ("证据审核员", "交付核对与今天先做三件事"),
}


def build_daily_assignment_digest(target_date: str) -> str:
    lines = [
        f"# {target_date} 新岗位任务拆解",
        "> AI 求职 Multi-Agent 已把日报转成可追踪任务图。"
        "依赖关系：岗位发现 → JD/知识分析 → 简历/作品/面试 → Judge。",
        "## 角色任务",
    ]
    for role_id, (display_name, assignment) in ROLE_DAILY_LABELS.items():
        dependencies = {
            "job_scout": "无，首先执行",
            "jd_analyst": "依赖岗位侦察员",
            "job_knowledge_curator": "依赖岗位侦察员",
            "resume_strategist": "依赖 JD 与岗位知识",
            "portfolio_coach": "依赖 JD 与岗位知识",
            "interview_coach": "依赖 JD 与岗位知识",
            "judge": "依赖全部工作角色",
        }[role_id]
        lines.append(
            f"- **{display_name}**（`{role_id}`）：{assignment}；{dependencies}。"
        )
    lines.extend(
        [
            "",
            "任务详情、依赖、模型和实时进度可在 Agent 小镇控制台查看；"
            "各角色机器人会继续在本群推送自己的完整分工。",
        ]
    )
    return "\n".join(lines)


def current_date() -> date:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date()


def parse_daily_command(
    text: str,
    *,
    today: date | None = None,
) -> tuple[str, str]:
    parts = text.strip().split()
    if not parts or parts[0] != "/daily":
        raise ValueError("用法：/daily [YYYY-MM-DD] [full]")

    target_date = (today or current_date()).isoformat()
    mode = "summary"
    for value in parts[1:]:
        if value in {"full", "完整"}:
            mode = "full"
        elif DATE_PATTERN.fullmatch(value):
            target_date = value
        else:
            raise ValueError("用法：/daily [YYYY-MM-DD] [full]")
    return target_date, mode


def split_sections(markdown: str) -> tuple[str, dict[str, list[str]]]:
    title = ""
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in markdown.splitlines():
        if not title and line.startswith("# "):
            title = line.removeprefix("# ").strip()
            continue
        match = SECTION_PATTERN.match(line)
        if match:
            current = match.group(1)
            sections[current] = []
            continue
        if current is not None:
            sections[current].append(line)
    return title, sections


def _clean_lines(lines: list[str]) -> list[str]:
    cleaned: list[str] = []
    for line in lines:
        stripped = line.strip()
        if "/Users/" in line:
            continue
        if stripped in {"阅读：", "补充阅读："}:
            continue
        cleaned.append(line.rstrip())
    while cleaned and not cleaned[0].strip():
        cleaned.pop(0)
    while cleaned and not cleaned[-1].strip():
        cleaned.pop()
    return cleaned


def _learning_summary(lines: list[str]) -> list[str]:
    cleaned = _clean_lines(lines)
    selected: list[str] = []
    capture_bullets = False
    prefixes = (
        "今日主题：",
        "今日题目：",
        "具体题目：",
        "具体阅读：",
    )
    for line in cleaned:
        stripped = line.strip()
        if stripped == "今天具体读：":
            capture_bullets = True
            continue
        if capture_bullets:
            if stripped.startswith("- "):
                selected.append(stripped)
                continue
            if stripped:
                capture_bullets = False
        if stripped.startswith(prefixes):
            selected.append(f"- {stripped}")
    return list(dict.fromkeys(selected))


def build_daily_digest(markdown: str, target_date: str) -> str:
    _, sections = split_sections(markdown)
    blocks = [
        f"# {target_date} AI 求职执行简报",
        "> 日报内容已直接推送到飞书，无需再打开本地文件。",
    ]

    autumn = _clean_lines(sections.get("今日秋招维护结果", []))
    if autumn:
        blocks.extend(["## 岗位与投递", "\n".join(autumn)])

    learning_blocks: list[str] = []
    for section_name, display_name in LEARNING_SECTIONS.items():
        items = _learning_summary(sections.get(section_name, []))
        if items:
            learning_blocks.append(f"### {display_name}\n" + "\n".join(items))
    if learning_blocks:
        blocks.extend(["## 今日具体学习", "\n\n".join(learning_blocks)])

    ideas = _clean_lines(sections.get("今日 Vibe Coding 灵感", []))
    portfolio = _clean_lines(sections.get("今日作品推进", []))
    if ideas or portfolio:
        blocks.append("## 创意与作品推进")
        if ideas:
            blocks.append("### Vibe Coding\n" + "\n".join(ideas))
        if portfolio:
            blocks.append("### 作品进展\n" + "\n".join(portfolio))

    priorities = _clean_lines(sections.get("今天先做三件事", []))
    if priorities:
        blocks.extend(["## 今天先做三件事", "\n".join(priorities)])

    return "\n\n".join(block for block in blocks if block.strip())


def build_full_daily(markdown: str, target_date: str) -> str:
    cleaned = _clean_lines(markdown.splitlines())
    if cleaned and cleaned[0].startswith("# "):
        cleaned[0] = f"# {target_date} AI 求职完整日报"
    return "\n".join(cleaned)


def _section_markdown(
    sections: dict[str, list[str]],
    section_name: str,
    display_name: str | None = None,
    *,
    include_terms: tuple[str, ...] = (),
) -> str:
    lines = _clean_lines(sections.get(section_name, []))
    if include_terms:
        lines = [
            line
            for line in lines
            if not line.strip()
            or any(term in line for term in include_terms)
        ]
    if not any(line.strip() for line in lines):
        return ""
    return f"## {display_name or section_name}\n\n" + "\n".join(lines)


def _role_header(role_id: str, target_date: str) -> list[str]:
    display_name, assignment = ROLE_DAILY_LABELS[role_id]
    return [
        f"# {display_name} · {target_date}",
        f"> 今日分工：{assignment}。内容由 `{role_id}` 身份独立推送。",
    ]


def build_role_daily_digests(
    markdown: str,
    target_date: str,
) -> dict[str, str]:
    """Partition one daily report into seven role-owned Feishu messages."""

    _, sections = split_sections(markdown)
    autumn = "今日秋招维护结果"
    concrete_learning = "今日具体学习与产出"
    learning_source = (
        concrete_learning
        if any(line.strip() for line in sections.get(concrete_learning, []))
        else "今日 AI / Agent 学习任务"
    )
    priorities = "今天先做三件事"

    blocks: dict[str, list[str]] = {
        "job_scout": [
            _section_markdown(
                sections,
                autumn,
                "今日岗位雷达",
                include_terms=(
                    "新增",
                    "最优先",
                    "其次",
                    "投递",
                    "截止",
                    "岗位",
                ),
            )
        ],
        "jd_analyst": [
            _section_markdown(
                sections,
                autumn,
                "JD 匹配与缺口",
                include_terms=(
                    "最优先",
                    "贴合",
                    "关键词",
                    "需要补强",
                    "方向补",
                ),
            ),
            _section_markdown(
                sections,
                "今日简历优化动作",
                "今天的 JD 拆解动作",
            ),
        ],
        "job_knowledge_curator": [
            _section_markdown(
                sections,
                learning_source,
                "今天具体学什么",
            )
        ],
        "resume_strategist": [
            _section_markdown(
                sections,
                autumn,
                "简历版本决策",
                include_terms=(
                    "优先简历",
                    "Agent 版",
                    "Algorithm 版",
                    "Data Science 版",
                    "关键词",
                    "bullet",
                ),
            ),
            _section_markdown(
                sections,
                "今日简历优化动作",
                "今天的简历动作",
            ),
        ],
        "portfolio_coach": [
            _section_markdown(
                sections,
                "今日 Vibe Coding 灵感",
                "Vibe Coding Idea",
            ),
            _section_markdown(
                sections,
                "今日作品推进",
                "作品推进与验收",
            ),
        ],
        "interview_coach": [
            _section_markdown(
                sections,
                learning_source,
                "今日面试训练",
                include_terms=(
                    "主线书",
                    "配套书",
                    "Agent 支线",
                    "SQL",
                    "LeetCode",
                    "ML 面试题",
                    "概率统计",
                    "口述",
                    "追问",
                ),
            ),
            _section_markdown(
                sections,
                "今日模拟面试",
                "模拟面试动作",
            ),
        ],
        "judge": [
            _section_markdown(
                sections,
                autumn,
                "关键结论复核",
            ),
            _section_markdown(
                sections,
                priorities,
                "今天先做三件事",
            ),
        ],
    }

    rendered: dict[str, str] = {}
    for role_id, role_blocks in blocks.items():
        nonempty = [block for block in role_blocks if block.strip()]
        if not nonempty:
            nonempty = ["今天的日报没有识别到该角色对应的新内容。"]
        rendered[role_id] = "\n\n".join(
            [*_role_header(role_id, target_date), *nonempty]
        )
    return rendered


def split_markdown(markdown: str, max_chars: int = 6000) -> list[str]:
    if len(markdown) <= max_chars:
        return [markdown]

    chunks: list[str] = []
    current: list[str] = []
    current_length = 0
    for block in re.split(r"\n(?=##\s)", markdown):
        if current and current_length + len(block) + 1 > max_chars:
            chunks.append("\n".join(current).strip())
            current = []
            current_length = 0
        if len(block) > max_chars:
            for paragraph in block.split("\n\n"):
                if current and current_length + len(paragraph) + 2 > max_chars:
                    chunks.append("\n".join(current).strip())
                    current = []
                    current_length = 0
                current.append(paragraph)
                current_length += len(paragraph) + 2
        else:
            current.append(block)
            current_length += len(block) + 1
    if current:
        chunks.append("\n".join(current).strip())
    return [chunk for chunk in chunks if chunk]


def load_daily_messages(
    prepare_dir: Path,
    target_date: str,
    mode: str = "summary",
    max_chars: int = 6000,
) -> list[str]:
    if not DATE_PATTERN.fullmatch(target_date):
        raise ValueError("日期必须是 YYYY-MM-DD")
    path = prepare_dir / "daily" / f"{target_date}.md"
    if not path.exists():
        raise FileNotFoundError(f"没有找到 {target_date} 的日报")
    markdown = path.read_text(encoding="utf-8")
    rendered = (
        build_full_daily(markdown, target_date)
        if mode == "full"
        else build_daily_digest(markdown, target_date)
    )
    return split_markdown(rendered, max_chars=max_chars)


def load_role_daily_messages(
    prepare_dir: Path,
    target_date: str,
    max_chars: int = 6000,
) -> dict[str, list[str]]:
    if not DATE_PATTERN.fullmatch(target_date):
        raise ValueError("日期必须是 YYYY-MM-DD")
    path = prepare_dir / "daily" / f"{target_date}.md"
    if not path.exists():
        raise FileNotFoundError(f"没有找到 {target_date} 的日报")
    rendered = build_role_daily_digests(
        path.read_text(encoding="utf-8"),
        target_date,
    )
    return {
        role_id: split_markdown(message, max_chars=max_chars)
        for role_id, message in rendered.items()
    }


def remember_chat_id(data_dir: Path, chat_id: str) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"chat_id": chat_id}, ensure_ascii=False) + "\n"
    fd, temp_name = tempfile.mkstemp(
        dir=data_dir,
        prefix=".feishu-target.",
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(temp_name, data_dir / "feishu_target.json")
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def load_chat_id(data_dir: Path) -> str:
    path = data_dir / "feishu_target.json"
    if not path.exists():
        raise FileNotFoundError(
            "尚未记录飞书会话；请先在目标会话中给机器人发送一条消息"
        )
    value = json.loads(path.read_text(encoding="utf-8")).get("chat_id", "")
    if not value:
        raise ValueError("已记录的飞书会话无效")
    return value
