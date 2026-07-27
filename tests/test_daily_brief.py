from datetime import date

import pytest

from job_agent_harness.daily_brief import (
    build_daily_digest,
    build_role_daily_digests,
    load_chat_id,
    load_daily_messages,
    load_role_daily_messages,
    parse_daily_command,
    remember_chat_id,
    split_markdown,
)


SAMPLE_DAILY = """# 2026-07-26 AI 求职准备日报

## 今日 AI / Agent 学习任务

阅读：

- /Users/example/agent.md

今日主题：Agent Evaluation

今天具体读：

- 学习工具调用；输出一张状态图。

## 今日秋招维护结果

- 新增：[目标岗位](https://example.com/job)，使用 Agent 版简历。
- 需要补强：golden set。

## 今日 Vibe Coding 灵感

- 首选：Trace 回归器。

## 今日作品推进

- 下一步：实现 trace_parser.py。

## 今天先做三件事

1. 投递目标岗位。
2. 完成评测学习。
3. 实现解析器。
"""


def test_parse_daily_command_defaults_and_full_mode():
    today = date(2026, 7, 26)

    assert parse_daily_command("/daily", today=today) == (
        "2026-07-26",
        "summary",
    )
    assert parse_daily_command(
        "/daily 2026-07-25 full",
        today=today,
    ) == ("2026-07-25", "full")


def test_parse_daily_command_rejects_unknown_arguments():
    with pytest.raises(ValueError, match="用法"):
        parse_daily_command("/daily tomorrow")


def test_daily_digest_contains_content_not_local_paths():
    digest = build_daily_digest(SAMPLE_DAILY, "2026-07-26")

    assert "目标岗位" in digest
    assert "Agent Evaluation" in digest
    assert "Trace 回归器" in digest
    assert "今天先做三件事" in digest
    assert "/Users/" not in digest


def test_load_daily_messages_and_split(tmp_path):
    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    (daily_dir / "2026-07-26.md").write_text(
        SAMPLE_DAILY,
        encoding="utf-8",
    )

    messages = load_daily_messages(
        tmp_path,
        "2026-07-26",
        max_chars=200,
    )

    assert len(messages) >= 2
    assert all(len(message) <= 200 for message in messages)
    assert split_markdown("short", max_chars=10) == ["short"]


def test_chat_id_is_remembered_without_entering_source_control(tmp_path):
    remember_chat_id(tmp_path, "oc_test")

    assert load_chat_id(tmp_path) == "oc_test"


def test_daily_report_is_partitioned_across_all_seven_role_bots(tmp_path):
    role_digests = build_role_daily_digests(SAMPLE_DAILY, "2026-07-26")

    assert set(role_digests) == {
        "job_scout",
        "jd_analyst",
        "job_knowledge_curator",
        "resume_strategist",
        "portfolio_coach",
        "interview_coach",
        "judge",
    }
    assert "目标岗位" in role_digests["job_scout"]
    assert "Agent Evaluation" in role_digests["job_knowledge_curator"]
    assert "Trace 回归器" in role_digests["portfolio_coach"]
    assert "今天先做三件事" in role_digests["judge"]
    assert all("/Users/" not in message for message in role_digests.values())

    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    (daily_dir / "2026-07-26.md").write_text(
        SAMPLE_DAILY,
        encoding="utf-8",
    )
    messages = load_role_daily_messages(
        tmp_path,
        "2026-07-26",
        max_chars=200,
    )
    assert set(messages) == set(role_digests)
    assert all(parts for parts in messages.values())
