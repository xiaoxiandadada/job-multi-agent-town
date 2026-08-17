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


def test_daily_report_is_partitioned_across_every_role_bot(tmp_path):
    role_digests = build_role_daily_digests(SAMPLE_DAILY, "2026-07-26")

    assert set(role_digests) == {
        "job_scout",
        "job_analyst",
        "match_scorer",
        "material_builder",
        "interview_coach",
        "judge",
    }
    assert "目标岗位" in role_digests["job_scout"]
    assert "Agent Evaluation" in role_digests["job_analyst"]
    assert "Trace 回归器" in role_digests["material_builder"]
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


def test_split_markdown_never_leaves_a_code_fence_open():
    """A section boundary can fall inside a fenced block.

    Feishu then renders the remaining sections as one grey code box, which is
    how a whole daily brief turned into monospace.
    """

    from job_agent_harness.daily_brief import split_markdown

    body = "\n".join(
        [
            "## 岗位 JD",
            "```text",
            *[f"第 {index} 行 JD 正文，足够长以便触发切分。" for index in range(60)],
            "```",
            "## 今日结论",
            "先投拼多多。",
        ]
    )

    chunks = split_markdown(body, max_chars=600)

    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.count("```") % 2 == 0, chunk
    assert "先投拼多多。" in chunks[-1]
    assert chunks[-1].count("```") == 0 or not chunks[-1].startswith("```text")


def test_rebalance_reopens_the_block_with_its_language():
    from job_agent_harness.daily_brief import rebalance_code_fences

    chunks = rebalance_code_fences(["## A\n```json\n{", '  "k": 1'])

    assert chunks[0].endswith("```")
    assert chunks[1].startswith("```json")
    assert chunks[1].endswith("```")
