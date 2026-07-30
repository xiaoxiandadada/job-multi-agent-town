from __future__ import annotations

import os
from pathlib import Path

from .models import RoleSpec


ROLE_CONTEXT_FILES = {
    "job_scout": [
        "jobs/autumn_job_tracker.md",
    ],
    "jd_analyst": [
        "jobs/autumn_job_tracker.md",
        "jobs/search_keywords.md",
    ],
    "job_knowledge_curator": [
        "books/book_based_learning_plan.md",
        "agent/agent_learning_path.md",
        "llm/rag_llm_learning_path.md",
    ],
    "resume_strategist": [
        "resume/resume_agent_version.md",
        "resume/resume_algorithm_version.md",
        "resume/resume_data_science_version.md",
        "portfolio/portfolio_roadmap.md",
    ],
    "portfolio_coach": [
        "portfolio/portfolio_roadmap.md",
        "ideas/vibe_coding_idea_bank.md",
    ],
    "interview_coach": [
        "books/book_based_learning_plan.md",
        "interview/mock_interview_bank.md",
    ],
    "judge": [
        "jobs/autumn_job_tracker.md",
        "portfolio/portfolio_roadmap.md",
    ],
}


def default_prepare_dir() -> Path:
    configured = os.getenv("JOB_AGENT_PREPARE_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().parents[4] / "prepare"


def latest_daily_path(prepare_dir: Path) -> Path | None:
    daily_dir = prepare_dir / "daily"
    if not daily_dir.is_dir():
        return None
    candidates = sorted(daily_dir.glob("????-??-??.md"))
    return candidates[-1] if candidates else None


def _excerpt(path: Path, max_chars: int) -> str:
    value = path.read_text(encoding="utf-8", errors="replace").strip()
    if len(value) <= max_chars:
        return value
    head_size = max_chars * 2 // 3
    tail_size = max_chars - head_size
    return (
        value[:head_size]
        + "\n\n[…中间内容已省略…]\n\n"
        + value[-tail_size:]
    )


def build_role_context(
    role: RoleSpec,
    *,
    prepare_dir: Path | None = None,
    max_chars: int = 18_000,
) -> str:
    """Load a bounded, auditable context pack for one specialist role."""

    root = prepare_dir or default_prepare_dir()
    candidates: list[Path] = []
    daily = latest_daily_path(root)
    if daily is not None:
        candidates.append(daily)
    candidates.extend(
        root / relative
        for relative in ROLE_CONTEXT_FILES.get(role.role_id, [])
    )
    existing = list(
        dict.fromkeys(path for path in candidates if path.is_file())
    )
    if not existing:
        return ""

    remaining = max(2000, max_chars)
    sections: list[str] = []
    for index, path in enumerate(existing):
        files_left = len(existing) - index
        budget = max(1200, remaining // files_left)
        content = _excerpt(path, budget)
        relative = (
            path.relative_to(root)
            if path.is_relative_to(root)
            else Path(path.name)
        )
        section = f"## {relative}\n{content}"
        sections.append(section)
        remaining = max(0, remaining - len(section))
        if remaining < 1200:
            break
    return "\n\n".join(sections)
