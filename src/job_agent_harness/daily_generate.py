"""When nobody wrote today's brief, the roles write it themselves.

``push_daily`` delivers a brief; it never authored one. The markdown it reads
comes from outside this repo (``<prepare>/daily/<date>.md``), so on any morning
that file is missing the whole push dies with ``FileNotFoundError`` — which is
exactly what a scheduled push must not do.

So this module produces the same markdown from real work: one single-role run per
section, each asked for the section its owner already owns downstream in
``daily_brief.build_role_daily_digests``. Assembling the file from per-role
outputs rather than asking one model to emit the whole document is deliberate —
the section headings are a contract the digest parser reads by exact string, and
a model paraphrasing one heading would silently empty a role's message.

The result is cached under the runtime data directory, never written into the
user's own ``prepare/`` tree: that folder is theirs, and a generated brief must
not shadow or race the real one.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .models import RunRequest


LOGGER = logging.getLogger("job_agent_harness.daily_generate")


@dataclass(frozen=True)
class DailySection:
    """One ``##`` block of the brief and the role that has to fill it.

    ``heading`` is matched verbatim by ``daily_brief.split_sections``; do not
    reword it without changing the digest builders in the same commit.
    """

    heading: str
    role_id: str
    ask: str


#: Ordered the way the file itself reads, and the way the work depends: the scout
#: names the jobs before anyone can analyse, tailor or rehearse against them.
DAILY_SECTIONS: tuple[DailySection, ...] = (
    DailySection(
        heading="今日秋招维护结果",
        role_id="job_scout",
        ask=(
            "核验今天面向 2027 届的 AI / Agent 方向正式校招岗位。逐条给出公司、"
            "岗位全名、官方链接、是否仍开放、网申截止时间，并标注哪一个最优先投递、"
            "哪些其次。每条都要能追到 JD 原文；查不到就写「查不到」，不要补足数量。"
            "同时点出这些岗位需要补强的关键词和最贴合的简历版本。"
        ),
    ),
    DailySection(
        heading="今日 AI / Agent 学习任务",
        role_id="job_analyst",
        ask=(
            "针对上面这些岗位，安排今天一天能做完的学习任务。分成主线书章节、"
            "配套题目（SQL / LeetCode / ML 面试题 / 概率统计各一道）两部分，"
            "每项写清具体章节号或题号、要产出什么、预计耗时。不要给书单，给今天读哪几页。"
        ),
    ),
    DailySection(
        heading="今日简历优化动作",
        role_id="material_builder",
        ask=(
            "针对最优先的那个岗位，给出今天要改的简历动作：选哪个简历版本、"
            "改写哪几条 bullet（给出改写前后）、补哪些关键词。只写今天做得完的。"
        ),
    ),
    DailySection(
        heading="今日 Vibe Coding 灵感",
        role_id="material_builder",
        ask=(
            "给出 1-2 个今天能动手、和上面岗位直接相关的 Vibe Coding 点子，"
            "每个写清要验证什么、最小可交付是什么、怎么算做完。"
        ),
    ),
    DailySection(
        heading="今日作品推进",
        role_id="material_builder",
        ask=(
            "现有作品集今天推进哪一块：具体改哪个模块、验收标准是什么、"
            "能不能在今天之内截图或录屏证明它跑通了。"
        ),
    ),
    DailySection(
        heading="今日模拟面试",
        role_id="interview_coach",
        ask=(
            "出今天的模拟面试题：一道项目深挖题、一道技术追问题，"
            "各写出追问链条和口述要点（口述结构而不是完整答案）。"
        ),
    ),
    DailySection(
        heading="今天先做三件事",
        role_id="judge",
        ask=(
            "复核上面各角色给出的内容，挑出今天最该先做的三件事，按顺序编号，"
            "每件写清完成标准。有互相冲突或缺证据的地方直接指出来。"
        ),
    ),
)


def cached_daily_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "daily"


def cached_daily_path(data_dir: Path, target_date: str) -> Path:
    return cached_daily_dir(data_dir) / f"{target_date}.md"


def generation_timeout_seconds() -> float:
    return float(os.getenv("JOB_AGENT_DAILY_GENERATE_TIMEOUT_SECONDS", "300"))


def _section_prompt(section: DailySection, target_date: str) -> str:
    return (
        f"今天是 {target_date}。请写出求职日报中「{section.heading}」这一节的正文。\n\n"
        f"{section.ask}\n\n"
        "只输出这一节的正文，不要重复标题，不要写开场白或总结句，不要用一级标题。"
        "允许用列表和三级标题（###）。没有可核验的内容就明说没有。"
    )


async def generate_section(
    orchestrator,
    section: DailySection,
    target_date: str,
    *,
    timeout_seconds: float | None = None,
) -> str:
    """Run the owning role and return its text, or "" when it produced nothing.

    A failed section must not sink the whole brief: six good sections plus one
    honest gap is a usable morning; an exception here is a morning with silence.
    """

    report = await orchestrator.run(
        RunRequest(
            query=_section_prompt(section, target_date),
            requested_roles=[section.role_id],
            mode="single",
            use_judge=False,
            timeout_seconds=(
                timeout_seconds
                if timeout_seconds is not None
                else generation_timeout_seconds()
            ),
            # Nobody is watching this one. Seven of them fire back to back, and
            # without the mark each would grab the page's single "current run"
            # slot in turn. See ``town.background_run_ids``.
            origin="schedule",
        )
    )
    failed = [item for item in report.results if item.status != "ok"]
    if failed or not report.final_output.strip():
        LOGGER.warning(
            "日报「%s」这一节没写出来：%s",
            section.heading,
            "; ".join(
                f"{item.role_id}: {item.status} {item.error or ''}".strip()
                for item in failed
            )
            or "没有产出任何内容",
        )
        return ""
    return report.final_output.strip()


async def generate_daily_markdown(
    orchestrator,
    target_date: str,
    *,
    sections: Sequence[DailySection] = DAILY_SECTIONS,
    timeout_seconds: float | None = None,
) -> str:
    """Assemble a brief in the exact shape ``daily_brief`` knows how to read.

    Sections run one at a time, in order, because they build on each other: the
    later roles are supposed to be reacting to the jobs the scout just verified,
    and the shared memory store is what carries that context between runs.
    """

    blocks = [
        f"# {target_date} AI 求职准备日报",
        "> 今天没有人工日报，以下内容由各角色现场跑出来，每条都应能追到来源。",
    ]
    written = 0
    for section in sections:
        body = await generate_section(
            orchestrator,
            section,
            target_date,
            timeout_seconds=timeout_seconds,
        )
        if not body:
            continue
        blocks.append(f"## {section.heading}\n\n{body}")
        written += 1
    if not written:
        raise RuntimeError(
            f"{target_date} 的日报一节都没写出来，不推送空日报"
        )
    LOGGER.info(
        "已现场生成 %s 的日报：%s/%s 节",
        target_date,
        written,
        len(sections),
    )
    return "\n\n".join(blocks) + "\n"


def write_cached_daily(data_dir: Path, target_date: str, markdown: str) -> Path:
    path = cached_daily_path(data_dir, target_date)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.partial")
    temp.write_text(markdown, encoding="utf-8")
    # Replaced in one step so a reader never sees a brief that stops mid-section.
    os.replace(temp, path)
    return path


async def ensure_daily_markdown(
    orchestrator,
    target_date: str,
    *,
    prepare_dir: Path,
    data_dir: Path,
    sections: Sequence[DailySection] = DAILY_SECTIONS,
    timeout_seconds: float | None = None,
) -> Path | None:
    """Make sure *some* brief exists for the date. Returns the path it wrote.

    ``None`` means one was already there — the hand-written brief always wins,
    both because it is better and because regenerating it would burn seven model
    calls to replace something the user already prepared.
    """

    existing = Path(prepare_dir) / "daily" / f"{target_date}.md"
    if existing.exists():
        return None
    cached = cached_daily_path(data_dir, target_date)
    if cached.exists():
        return None
    markdown = await generate_daily_markdown(
        orchestrator,
        target_date,
        sections=sections,
        timeout_seconds=timeout_seconds,
    )
    return write_cached_daily(data_dir, target_date, markdown)


def main() -> None:
    """``job-agent-write-daily`` — generate today's brief without pushing it."""

    import argparse

    from .runtime import (
        build_memory_store,
        build_registry,
        build_orchestrator,
        prepare_directory,
        runtime_data_dir,
    )
    from .daily_brief import current_date

    parser = argparse.ArgumentParser(
        description="现场生成一份当天的求职日报，缓存到 runtime 目录，不推送。"
    )
    parser.add_argument("--date", dest="target_date")
    parser.add_argument(
        "--force",
        action="store_true",
        help="即使已有日报也重新生成（只覆盖 runtime 缓存，不动 prepare 目录）",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=os.getenv("JOB_AGENT_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    target_date = args.target_date or current_date().isoformat()
    registry = build_registry()
    orchestrator = build_orchestrator(registry, build_memory_store())

    async def run() -> None:
        if args.force:
            markdown = await generate_daily_markdown(orchestrator, target_date)
            path = write_cached_daily(runtime_data_dir(), target_date, markdown)
        else:
            path = await ensure_daily_markdown(
                orchestrator,
                target_date,
                prepare_dir=prepare_directory(),
                data_dir=runtime_data_dir(),
            )
        print(f"daily_brief={path or '已存在，未重新生成'}")

    asyncio.run(run())


if __name__ == "__main__":
    main()
