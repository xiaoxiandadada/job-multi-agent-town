"""When a fit score turns into a push, and when it stays quiet.

The alert is the one part of the scorer that reaches out on its own, so these
tests care less about wording than about the three independent reasons to stay
silent — below the bar, hard blocker, scorer said don't. Each is checked on its
own, because a gate that only works because another one happens to catch the
same case is a gate that disappears in the next refactor.

The blocker case is written with a high ``overall`` on purpose. ``MatchReport``
already caps a blocked report at 40, so a realistic blocked report would fail the
threshold check and never reach the blocker check at all — which would let the
blocker gate rot untested.
"""

import json

import pytest

from job_agent_harness.match_alert import (
    DEFAULT_THRESHOLD,
    alert_for,
    alert_from_results,
    alert_threshold,
    alerts_enabled,
    should_alert,
)
from job_agent_harness.models import (
    MATCH_DIMENSIONS,
    AgentResult,
    MatchDimension,
    MatchReport,
)
from job_agent_harness.structured import structured_result


@pytest.fixture(autouse=True)
def clean_alert_env(monkeypatch):
    """Every test states its own configuration; none inherits the shell's."""

    monkeypatch.delenv("JOB_AGENT_MATCH_ALERT", raising=False)
    monkeypatch.delenv("JOB_AGENT_MATCH_ALERT_THRESHOLD", raising=False)


def make_report(**overrides) -> MatchReport:
    """A report that clears every gate, so each test can break exactly one.

    ``overall`` is passed explicitly rather than derived through ``recomputed``:
    these tests are about the alert gate, and going through the scoring maths
    would couple them to a weighting change that has nothing to do with alerts.
    """

    payload = {
        "company": "字节跳动",
        "job_title": "AI Agent 工程师",
        "job_url": "https://jobs.example.com/agent-2027",
        "overall": 82,
        "verdict": "值得投",
        "dimensions": [
            MatchDimension(
                name=name,
                score=80,
                weight=0.2,
                evidence=f"{name} 有对应证据",
            )
            for name in MATCH_DIMENSIONS
        ],
        "blockers": [],
        "quick_wins": [],
    }
    payload.update(overrides)
    return MatchReport(**payload)


def test_default_threshold_is_the_number_the_user_asked_for():
    assert DEFAULT_THRESHOLD == 70
    assert alert_threshold() == 70


def test_a_score_exactly_at_the_threshold_fires():
    # "70% 就提醒" reads as inclusive, and a 70 that stayed silent would look
    # like the feature was broken rather than strict.
    assert should_alert(make_report(overall=70)) is True


def test_a_score_one_point_short_stays_quiet():
    assert should_alert(make_report(overall=69)) is False


def test_a_blocker_silences_even_a_high_score():
    report = make_report(overall=85, blockers=["只招 985/211"])
    assert should_alert(report) is False
    assert alert_for(report) is None


def test_a_do_not_apply_verdict_beats_the_arithmetic():
    report = make_report(overall=90, verdict="不建议")
    assert should_alert(report) is False


def test_threshold_is_configurable(monkeypatch):
    monkeypatch.setenv("JOB_AGENT_MATCH_ALERT_THRESHOLD", "85")
    assert alert_threshold() == 85
    assert should_alert(make_report(overall=82)) is False
    assert should_alert(make_report(overall=88)) is True


def test_a_malformed_threshold_falls_back_instead_of_raising(monkeypatch):
    monkeypatch.setenv("JOB_AGENT_MATCH_ALERT_THRESHOLD", "七十")
    assert alert_threshold() == DEFAULT_THRESHOLD
    assert should_alert(make_report(overall=75)) is True


def test_an_out_of_range_threshold_is_clamped(monkeypatch):
    monkeypatch.setenv("JOB_AGENT_MATCH_ALERT_THRESHOLD", "150")
    assert alert_threshold() == 100


def test_alerts_are_on_unless_switched_off(monkeypatch):
    assert alerts_enabled() is True
    monkeypatch.setenv("JOB_AGENT_MATCH_ALERT", "0")
    assert alerts_enabled() is False
    assert should_alert(make_report()) is False


def test_the_body_carries_the_number_the_job_and_the_link():
    body = alert_for(make_report())
    assert body is not None
    assert "82/100" in body
    assert "字节跳动" in body
    assert "AI Agent 工程师" in body
    # The link has to survive verbatim — a nudge whose link was reformatted into
    # prose is a nudge the user cannot act on from a notification.
    assert "https://jobs.example.com/agent-2027" in body


def test_the_body_names_the_strongest_and_weakest_axis():
    report = make_report(
        dimensions=[
            MatchDimension(
                name="硬性门槛", score=95, weight=0.2, evidence="2027 届本科符合"
            ),
            MatchDimension(
                name="核心技能",
                score=40,
                weight=0.2,
                evidence="只做过课程项目",
                gap="缺少线上 Agent 系统经验",
            ),
        ]
    )
    body = alert_for(report)
    assert body is not None
    assert "硬性门槛 95" in body
    assert "核心技能 40" in body
    # The weakest axis shows its gap rather than its evidence: the gap is the
    # thing the user can still act on before applying.
    assert "缺少线上 Agent 系统经验" in body


def test_one_dimension_is_not_reported_as_both_best_and_worst():
    report = make_report(
        dimensions=[
            MatchDimension(
                name="核心技能", score=80, weight=1.0, evidence="三个 Agent 项目"
            )
        ]
    )
    body = alert_for(report)
    assert body is not None
    assert body.count("核心技能 80") == 1
    assert "最弱项" not in body


def test_quick_wins_are_capped_so_the_push_stays_actionable():
    report = make_report(
        quick_wins=[f"动作 {index}" for index in range(6)],
    )
    body = alert_for(report)
    assert body is not None
    assert "动作 0" in body
    assert "动作 2" in body
    assert "动作 3" not in body


def test_a_long_reason_is_clipped_rather_than_pasted_whole():
    report = make_report(
        dimensions=[
            MatchDimension(
                name="核心技能",
                score=80,
                weight=0.5,
                evidence="证" * 400,
            ),
            MatchDimension(
                name="加分项", score=60, weight=0.5, evidence="次要证据"
            ),
        ]
    )
    body = alert_for(report)
    assert body is not None
    assert "…" in body
    assert "证" * 400 not in body


def test_alert_from_results_picks_the_scorer_out_of_a_run():
    results = [
        AgentResult(role_id="job_scout", display_name="Job Scout", output="岗位若干"),
        AgentResult(
            role_id="match_scorer",
            display_name="Match Scorer",
            output="渲染后的表格",
            match_report=make_report(),
        ),
    ]
    body = alert_from_results(results)
    assert body is not None
    assert "82/100" in body


def test_a_run_without_a_score_produces_no_alert():
    results = [
        AgentResult(role_id="job_scout", display_name="Job Scout", output="岗位若干"),
        AgentResult(
            role_id="job_analyst", display_name="Job Analyst", output="七节分析"
        ),
    ]
    assert alert_from_results(results) is None


def test_structured_result_hands_back_both_the_markdown_and_the_object():
    """The link between the scorer's JSON and the alert.

    Without the object surviving this step the threshold check would have to
    regex a rendered table, which is the failure this function exists to avoid.
    """

    payload = {
        "company": "字节跳动",
        "job_title": "AI Agent 工程师",
        "job_url": "https://jobs.example.com/agent-2027",
        "verdict": "值得投",
        "blockers": [],
        "quick_wins": ["补一个 RAG 评测脚本"],
        "dimensions": [
            {
                "name": name,
                "score": 80,
                "weight": 0.2,
                "evidence": f"{name} 的证据",
                "gap": "",
            }
            for name in MATCH_DIMENSIONS
        ],
    }
    rendered, report = structured_result("match_scorer", json.dumps(payload))
    assert report is not None
    assert report.overall == 80
    assert "匹配度 80/100" in rendered
    assert should_alert(report) is True


def test_structured_result_degrades_to_raw_text_and_no_object():
    rendered, report = structured_result("match_scorer", "模型这次没给 JSON")
    assert rendered == "模型这次没给 JSON"
    assert report is None


def test_structured_result_leaves_other_roles_alone():
    rendered, report = structured_result("job_analyst", "## 一、岗位目标")
    assert rendered == "## 一、岗位目标"
    assert report is None
