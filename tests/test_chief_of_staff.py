import pytest

from job_agent_harness.activity import ActivityStore
from job_agent_harness.chief_of_staff import (
    CHIEF_DISPLAY_NAME,
    CLOSING_HEADING,
    ChiefOfStaff,
    closing_enabled,
    dispatch_note,
    matched_keywords,
)
from job_agent_harness.model_client import MockModelClient
from job_agent_harness.models import ImageAttachment, RoleSpec, RunRequest
from job_agent_harness.orchestrator import MultiAgentOrchestrator
from job_agent_harness.registry import RoleRegistry


def make_role(role_id: str, keyword: str, display_name: str = "") -> RoleSpec:
    return RoleSpec(
        role_id=role_id,
        display_name=display_name or role_id,
        goal=f"完成 {keyword} 相关的求职任务",
        system_prompt=f"你负责 {keyword}，只输出可核验证据。",
        trigger_keywords=[keyword],
    )


def image(name: str = "jd.png") -> ImageAttachment:
    return ImageAttachment(
        media_type="image/png",
        data="aGVsbG8td29ybGQ=",
        source_name=name,
    )


@pytest.fixture
def parts(tmp_path):
    """A real orchestrator plus the activity log the page reads."""

    registry = RoleRegistry(tmp_path / "roles.json")
    registry.replace_all(
        [
            make_role("jd_analyst", "JD", "JD Analyst"),
            make_role("resume_strategist", "简历", "Resume Strategist"),
            make_role("judge", "不会自动触发", "Evidence Judge"),
        ]
    )
    activity = ActivityStore(tmp_path / "activity.jsonl", excerpt_chars=600)
    client = MockModelClient()
    orchestrator = MultiAgentOrchestrator(
        registry,
        client,
        activity_store=activity,
    )
    return orchestrator, client, activity


def kinds(activity: ActivityStore) -> list[str]:
    return [event.kind for event in activity.read(limit=200)]


def only(activity: ActivityStore, kind: str):
    found = [event for event in activity.read(limit=200) if event.kind == kind]
    assert len(found) == 1, f"{kind}: {len(found)} 条"
    return found[0]


def test_dispatch_note_explains_the_real_routing_decision():
    roles = [
        make_role("jd_analyst", "JD", "JD Analyst"),
        make_role("resume_strategist", "简历", "Resume Strategist"),
    ]

    note = dispatch_note(
        "分析这个 JD 并改我的简历",
        roles,
        mode="collaborative",
        image_count=2,
    )

    assert note.splitlines() == [
        f"📋 {CHIEF_DISPLAY_NAME} 已接单",
        "- 意图：分析这个 JD 并改我的简历",
        "- 附件：2 张图片",
        "- 分派：JD Analyst、Resume Strategist（分阶段协作）",
        "- 依据：JD Analyst←JD、Resume Strategist←简历",
        "- 收尾：Evidence Judge 审证据 → Chief of Staff 给下一步",
    ]


def test_dispatch_note_says_so_when_nothing_matched():
    note = dispatch_note(
        "随便聊聊",
        [make_role("jd_analyst", "JD", "JD Analyst")],
        mode="parallel",
        use_judge=False,
        with_closing=False,
    )

    assert "- 附件：无" in note
    assert "- 依据：无关键词命中，按默认角色兜底" in note
    assert "- 收尾：直接返回角色原文" in note


def test_dispatch_note_does_not_invent_keywords_for_an_explicit_request():
    note = dispatch_note(
        "帮我准备面试",
        [make_role("jd_analyst", "JD", "JD Analyst")],
        mode="single",
        requested_explicitly=True,
    )

    assert "- 依据：用户直接指定角色" in note


def test_matched_keywords_mirrors_the_router():
    role = make_role("jd_analyst", "JD")
    role.trigger_keywords = ["JD", "岗位", "简历"]

    assert matched_keywords(role, "看看这个 jd 的岗位要求") == ["JD", "岗位"]
    assert matched_keywords(role, "无关内容") == []


def test_closing_flag_is_on_unless_explicitly_turned_off():
    assert closing_enabled({}) is True
    assert closing_enabled({"JOB_AGENT_CHIEF_CLOSING": "1"}) is True
    for value in ("0", "false", "off", "no", " OFF "):
        assert closing_enabled({"JOB_AGENT_CHIEF_CLOSING": value}) is False


async def test_receipt_arrives_before_the_roles_start_working(parts):
    orchestrator, client, activity = parts
    notes: list[str] = []

    async def notify(note: str) -> None:
        # The original complaint was silence: the receipt has to be out the door
        # before any role has been asked anything.
        assert client.calls == []
        notes.append(note)

    chief = ChiefOfStaff(orchestrator, with_closing=False)
    await chief.run(
        RunRequest(query="分析 JD 并优化简历", use_judge=False),
        notify=notify,
    )

    assert len(notes) == 1
    assert notes[0].startswith(f"📋 {CHIEF_DISPLAY_NAME} 已接单")
    assert "JD Analyst、Resume Strategist" in notes[0]
    intake = only(activity, "intake_completed")
    assert intake.phase == "intake"
    assert intake.role_id == "controller"
    assert intake.selected_role_ids == ["jd_analyst", "resume_strategist"]


async def test_every_chief_event_joins_the_same_run(parts):
    orchestrator, _, activity = parts

    report = await ChiefOfStaff(orchestrator, with_closing=True).run(
        RunRequest(query="分析 JD 并优化简历", images=[image()]),
    )

    run_ids = {event.run_id for event in activity.read(limit=200)}
    assert run_ids == {report.run_id}
    assert kinds(activity).index("intake_completed") == 0
    assert kinds(activity)[-1] == "closing_created"


async def test_one_selected_role_looks_at_the_picture_itself(parts):
    orchestrator, client, activity = parts

    await ChiefOfStaff(orchestrator, with_closing=False).run(
        RunRequest(
            query="改我的简历",
            requested_roles=["resume_strategist"],
            mode="single",
            use_judge=False,
            images=[image()],
        )
    )

    # No transcription call, and the picture reached the role unchanged.
    assert client.image_calls == [("resume_strategist", 1)]
    read = only(activity, "attachment_read")
    assert read.status == "ok"
    assert read.metrics["transcribed"] is False
    assert "Resume Strategist" in (read.output_excerpt or "")


async def test_several_roles_share_one_transcription(parts):
    orchestrator, client, activity = parts

    report = await ChiefOfStaff(orchestrator, with_closing=False).run(
        RunRequest(query="分析 JD 并优化简历", use_judge=False, images=[image()])
    )

    # The chief pays the image tokens once; the roles get text.
    assert client.image_calls[0] == ("controller", 1)
    assert [count for _, count in client.image_calls[1:]] == [0, 0]
    read = only(activity, "attachment_read")
    assert read.metrics["image_count"] == 1
    analyst_query = next(
        query for role_id, query in client.queries if role_id == "jd_analyst"
    )
    assert "用户附件" in analyst_query
    assert report.metrics.model_calls == 3


async def test_a_failed_transcription_hands_the_raw_pictures_over_instead(parts):
    orchestrator, client, activity = parts
    notes: list[str] = []

    async def boom(role, query, *, images=()):
        if role.role_id == "controller":
            raise RuntimeError("vision 502")
        return await MockModelClient.complete(client, role, query, images=images)

    orchestrator.model_client = type(
        "Flaky",
        (),
        {
            "complete": staticmethod(boom),
            "model_for": staticmethod(lambda role: "mock"),
        },
    )()

    async def notify(note: str) -> None:
        notes.append(note)

    await ChiefOfStaff(orchestrator, with_closing=False).run(
        RunRequest(query="分析 JD 并优化简历", use_judge=False, images=[image()]),
        notify=notify,
    )

    read = only(activity, "attachment_read")
    assert read.status == "error"
    assert "vision 502" in (read.error or "")
    # Fail soft: the roles still see the picture rather than losing the run.
    assert [count for _, count in client.image_calls] == [1, 1]
    assert any("读图失败" in note for note in notes)


async def test_closing_is_appended_under_its_own_heading(parts):
    orchestrator, client, activity = parts

    report = await ChiefOfStaff(orchestrator, with_closing=True).run(
        RunRequest(query="分析 JD 并优化简历", use_judge=False)
    )

    assert CLOSING_HEADING in report.final_output
    assert client.calls[-1] == "controller"
    closing = only(activity, "closing_created")
    assert closing.phase == "delivery"
    assert closing.role_id == "controller"
    # The closing prompt may only quote what the run already produced.
    closing_query = client.queries[-1][1]
    assert "本轮已经交付并审核过的内容" in closing_query


async def test_closing_can_be_turned_off_without_losing_the_receipt(parts):
    orchestrator, client, activity = parts

    report = await ChiefOfStaff(orchestrator, with_closing=False).run(
        RunRequest(query="分析 JD 并优化简历", use_judge=False)
    )

    assert CLOSING_HEADING not in report.final_output
    assert "closing_created" not in kinds(activity)
    assert "intake_completed" in kinds(activity)
    assert client.calls == ["jd_analyst", "resume_strategist"]


async def test_the_chiefs_own_calls_show_up_in_the_metrics(parts):
    orchestrator, _, _ = parts
    request = RunRequest(query="分析 JD 并优化简历", use_judge=False, images=[image()])

    plain = await orchestrator.run(request)
    chief_run = await ChiefOfStaff(orchestrator, with_closing=True).run(request)

    # Reading the picture and writing the closing are two more calls, and the
    # report must not pretend they were free.
    assert chief_run.metrics.model_calls == plain.metrics.model_calls + 2
    assert chief_run.metrics.output_tokens > plain.metrics.output_tokens


async def test_a_failed_closing_still_returns_the_audited_answer(parts):
    orchestrator, client, activity = parts
    calls: list[str] = []

    async def flaky(role, query, *, images=()):
        calls.append(role.role_id)
        if role.role_id == "controller":
            raise TimeoutError("closing timed out")
        return await MockModelClient.complete(client, role, query, images=images)

    orchestrator.model_client = type(
        "Flaky",
        (),
        {
            "complete": staticmethod(flaky),
            "model_for": staticmethod(lambda role: "mock"),
        },
    )()

    report = await ChiefOfStaff(orchestrator, with_closing=True).run(
        RunRequest(query="分析 JD 并优化简历", use_judge=False)
    )

    assert CLOSING_HEADING not in report.final_output
    assert "[JD Analyst]" in report.final_output
    assert only(activity, "closing_created").status == "error"
