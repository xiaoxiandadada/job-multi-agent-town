import pytest

from job_agent_harness.daily_brief import build_role_daily_digests
from job_agent_harness.daily_generate import (
    DAILY_SECTIONS,
    DailySection,
    ensure_daily_markdown,
    generate_daily_markdown,
)
from job_agent_harness.models import AgentResult, RunMetrics, RunReport


class FakeOrchestrator:
    """Answers every section with text naming the role that was asked.

    ``dead_roles`` fail the way a gateway timeout does — the run comes back
    "completed" with a failed result inside it, which is the shape the real
    orchestrator returns and the one a naive check misses.
    """

    def __init__(self, *, dead_roles: set[str] | None = None):
        self.requests = []
        self.dead_roles = dead_roles or set()

    async def run(self, request):
        self.requests.append(request)
        role_id = request.requested_roles[0]
        failed = role_id in self.dead_roles
        return RunReport(
            run_id=f"run-{len(self.requests)}",
            query=request.query,
            mode=request.mode,
            role_registry_version=1,
            results=[
                AgentResult(
                    role_id=role_id,
                    display_name=role_id,
                    output="" if failed else f"{role_id} 的可核验产出",
                    status="timeout" if failed else "ok",
                    error="agent timed out" if failed else None,
                )
            ],
            final_output=(
                ""
                if failed
                else (
                    f"- 今日主题：{role_id} 负责的内容\n"
                    "- 最优先投递：ShadowWeave Agent Memory 岗位，截止 08-20\n"
                    "- 具体题目：LeetCode 146 LRU Cache"
                )
            ),
            metrics=RunMetrics(
                selected_roles=1,
                completed_roles=0 if failed else 1,
                failed_roles=1 if failed else 0,
                model_calls=1,
                wall_latency_ms=1200,
                sum_agent_latency_ms=1100,
                input_tokens=100,
                output_tokens=200,
                parallel_speedup_estimate=1.0,
            ),
        )


async def test_a_generated_brief_gives_every_role_something_to_push():
    """The real contract: the digest builders must recognise what we wrote.

    ``build_role_daily_digests`` matches ``##`` headings by exact string, so a
    generated brief whose headings drift produces one message per role that says
    "今天的日报没有识别到该角色对应的新内容" — a push that technically succeeds and
    tells the user nothing.
    """

    orchestrator = FakeOrchestrator()

    markdown = await generate_daily_markdown(orchestrator, "2026-08-05")
    digests = build_role_daily_digests(markdown, "2026-08-05")

    assert len(digests) == 6
    for role_id, message in digests.items():
        assert "没有识别到该角色对应的新内容" not in message, role_id


async def test_every_generation_run_is_marked_as_background_work():
    orchestrator = FakeOrchestrator()

    await generate_daily_markdown(orchestrator, "2026-08-05")

    assert len(orchestrator.requests) == len(DAILY_SECTIONS)
    # One single-role run per section, back to back. Unmarked, each would take
    # over the page's one "current run" slot (see town.background_run_ids).
    assert {request.origin for request in orchestrator.requests} == {"schedule"}
    assert {request.mode for request in orchestrator.requests} == {"single"}
    assert not any(request.use_judge for request in orchestrator.requests)


async def test_one_dead_section_does_not_sink_the_whole_brief():
    orchestrator = FakeOrchestrator(dead_roles={"interview_coach"})

    markdown = await generate_daily_markdown(orchestrator, "2026-08-05")

    assert "## 今日模拟面试" not in markdown
    # Six good sections plus one honest gap beats a morning of silence.
    assert "## 今日秋招维护结果" in markdown
    assert "## 今天先做三件事" in markdown


async def test_nothing_is_written_when_no_section_survived():
    orchestrator = FakeOrchestrator(
        dead_roles={section.role_id for section in DAILY_SECTIONS}
    )

    with pytest.raises(RuntimeError, match="一节都没写出来"):
        await generate_daily_markdown(orchestrator, "2026-08-05")


async def test_a_handwritten_brief_is_never_regenerated(tmp_path):
    prepare = tmp_path / "prepare"
    (prepare / "daily").mkdir(parents=True)
    (prepare / "daily" / "2026-08-05.md").write_text(
        "# 2026-08-05 AI 求职准备日报\n", encoding="utf-8"
    )
    orchestrator = FakeOrchestrator()

    written = await ensure_daily_markdown(
        orchestrator,
        "2026-08-05",
        prepare_dir=prepare,
        data_dir=tmp_path / "runtime",
    )

    # The user's own brief wins, and regenerating it would burn seven model
    # calls to replace something better than what we would write.
    assert written is None
    assert orchestrator.requests == []


async def test_a_generated_brief_lands_in_the_runtime_dir_not_the_users(
    tmp_path,
):
    prepare = tmp_path / "prepare"
    (prepare / "daily").mkdir(parents=True)
    data_dir = tmp_path / "runtime"
    orchestrator = FakeOrchestrator()

    written = await ensure_daily_markdown(
        orchestrator,
        "2026-08-05",
        prepare_dir=prepare,
        data_dir=data_dir,
    )

    assert written == data_dir / "daily" / "2026-08-05.md"
    assert written.read_text(encoding="utf-8").startswith("# 2026-08-05")
    # `prepare/` belongs to the user; a machine-written stand-in must not sit in
    # it, where it would shadow or race the real generator.
    assert list((prepare / "daily").iterdir()) == []

    # Second call the same day reuses the cache instead of paying again.
    orchestrator.requests.clear()
    assert (
        await ensure_daily_markdown(
            orchestrator,
            "2026-08-05",
            prepare_dir=prepare,
            data_dir=data_dir,
        )
        is None
    )
    assert orchestrator.requests == []


async def test_a_custom_section_list_is_honoured():
    orchestrator = FakeOrchestrator()
    sections = (
        DailySection(
            heading="今天先做三件事",
            role_id="judge",
            ask="挑出今天最该先做的三件事。",
        ),
    )

    markdown = await generate_daily_markdown(
        orchestrator,
        "2026-08-05",
        sections=sections,
    )

    assert markdown.count("## ") == 1
    assert len(orchestrator.requests) == 1
