"""Deciding when a fit score is worth interrupting someone for.

The scorer already produces a number and the evidence behind it. What it does
not do is *reach out* — the report sits in whatever channel asked for it, so a
strong match found during an unattended patrol is only discovered next time the
user opens the chat. This module is the one place that decides "this score
deserves a push", and it deliberately owns nothing else: no transport, no
formatting of the full report, no scoring.

The bar is set by ``MatchReport.overall``, but a threshold alone is not enough to
be trustworthy. Three separate things have to be true before an alert fires, and
each rules out a different way a nudge can be wrong:

* the score clears the threshold — the user's actual criterion;
* there are no ``blockers`` — a hard stop makes the score irrelevant, and telling
  someone to apply for a role that requires a graduation year they do not have is
  worse than staying quiet;
* the verdict is not ``不建议`` — when the scorer's own conclusion contradicts its
  arithmetic, the conclusion wins.

Note that the blocker check is not redundant with the threshold even though
``MatchReport.recomputed`` already caps a blocked report at 40. That cap is an
implementation detail of the scoring maths and could reasonably change; a nudge
that only stays correct because of a constant in another module is one refactor
away from recommending a job the user is ineligible for.
"""

from __future__ import annotations

import os
from collections.abc import Iterable

from .models import AgentResult, MatchReport

#: Percent. The user's stated bar: "70% 就提醒我投递".
DEFAULT_THRESHOLD = 70

#: Evidence and gap strings are allowed 600 characters in ``MatchDimension``,
#: which is right for a full report and far too long for a push notification.
_REASON_MAX_CHARS = 120

#: A nudge is a prompt to act, not a second copy of the report. Past three the
#: user is reading a list instead of doing the first thing on it.
_MAX_QUICK_WINS = 3


def alerts_enabled() -> bool:
    """Whether apply-nudges are on. Unset means on.

    Defaulting to on is the opposite of how the other optional integrations here
    behave, and it is deliberate: this is a feature someone asked for by name,
    so the surprising outcome would be configuring a threshold and hearing
    nothing back.
    """

    raw = os.getenv("JOB_AGENT_MATCH_ALERT", "").strip().lower()
    return raw in {"", "1", "true", "yes", "on"}


def alert_threshold() -> int:
    """The score at or above which a match is worth a push.

    A malformed value falls back to the default rather than raising. The
    alternative is a typo in an env file taking down every run that happens to
    score a job, which is a lot of damage for a stray character.
    """

    raw = os.getenv("JOB_AGENT_MATCH_ALERT_THRESHOLD", "").strip()
    if not raw:
        return DEFAULT_THRESHOLD
    try:
        value = int(float(raw))
    except ValueError:
        return DEFAULT_THRESHOLD
    return max(0, min(100, value))


def _clip(text: str) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= _REASON_MAX_CHARS:
        return collapsed
    return collapsed[: _REASON_MAX_CHARS - 1] + "…"


def should_alert(report: MatchReport) -> bool:
    """The three-part gate, separated from formatting so it can be tested alone."""

    if not alerts_enabled():
        return False
    if report.overall < alert_threshold():
        return False
    if report.blockers:
        return False
    return report.verdict != "不建议"


def render_alert(report: MatchReport) -> str:
    """The push body: the number, the job, the link, and what to do about it.

    Structured to be actionable on a phone lock screen — the link is on its own
    line so it stays tappable, and the strongest and weakest axes are named so
    the user can sanity-check the number without opening the full report. The
    scorer's own report is still sent separately; this does not replace it.
    """

    ranked = sorted(report.dimensions, key=lambda item: item.score, reverse=True)
    lines = [
        f"🎯 **匹配度 {report.overall}/100 · {report.verdict}** —— 建议投递",
        f"{report.company} · {report.job_title}",
    ]
    if report.job_url:
        lines.append(report.job_url)
    lines.append("")
    strongest = ranked[0]
    lines.append(
        f"- 最强项 **{strongest.name} {strongest.score}**："
        f"{_clip(strongest.evidence)}"
    )
    # With one dimension the strongest and weakest are the same axis; printing it
    # twice reads like a bug even though the arithmetic is fine.
    if len(ranked) > 1:
        weakest = ranked[-1]
        detail = _clip(weakest.gap) if weakest.gap else _clip(weakest.evidence)
        lines.append(f"- 最弱项 **{weakest.name} {weakest.score}**：{detail}")
    if report.quick_wins:
        lines.append("")
        lines.append("**投递前顺手做掉**")
        lines.extend(f"- {_clip(win)}" for win in report.quick_wins[:_MAX_QUICK_WINS])
    return "\n".join(lines)


def alert_for(report: MatchReport) -> str | None:
    """The nudge for one report, or ``None`` when it does not clear the gate."""

    return render_alert(report) if should_alert(report) else None


def alert_from_results(results: Iterable[AgentResult]) -> str | None:
    """Scan a run's results for a match worth pushing.

    A run scores at most one job, so the first qualifying report is the answer.
    Written as a scan anyway because the caller holds ``report.results`` and
    should not have to know which role carries the score.
    """

    for result in results:
        report = result.match_report
        if report is not None and should_alert(report):
            return render_alert(report)
    return None
