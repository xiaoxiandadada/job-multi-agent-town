"""Turning a model's text reply into validated data.

Two consumers, one problem: the planner needs a decomposition object and the
match scorer needs a score report, and both arrive as text that is *supposed* to
be JSON. ``extract_json_object`` lives here rather than next to either of them
because it belongs to neither.

The parsers deliberately never raise. A malformed reply must degrade to a
documented fallback — keyword routing for the planner, "no score this run" for
the scorer — rather than take down a run the user is waiting on.
"""

from __future__ import annotations

import json
import re

from .models import MATCH_DIMENSIONS, MatchDimension, MatchReport


def extract_json_object(text: str) -> dict | None:
    """Pull the first complete JSON object out of a model reply.

    Models wrap JSON in prose or fences even when told not to, so a bare
    ``json.loads`` on the whole reply is not enough. Scans for a
    brace-balanced span and ignores braces inside strings, which a
    ``text[first:last]`` slice gets wrong as soon as the reply contains two
    objects or a trailing sentence with a brace in it.
    """

    stripped = text.strip()
    fenced = re.search(
        r"```(?:json)?\s*(.+?)\s*```",
        stripped,
        re.DOTALL,
    )
    if fenced:
        stripped = fenced.group(1).strip()
    start = stripped.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(stripped)):
        character = stripped[index]
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                try:
                    payload = json.loads(stripped[start : index + 1])
                except ValueError:
                    return None
                return payload if isinstance(payload, dict) else None
    return None


def parse_match_report(text: str) -> MatchReport | None:
    """Turn a scorer reply into a report, or ``None`` to show no score.

    ``output_config.format`` makes malformed JSON unlikely but not impossible —
    a truncated reply (``stop_reason: max_tokens``) is still cut mid-object. And
    the schema cannot express everything the report needs, so the rest is checked
    here: score ranges, and that the fixed dimension set is actually complete.

    A partial dimension set is rejected rather than scored. The whole point of
    fixing the five axes is cross-job comparability, and a report missing 硬性门槛
    would quietly rank against reports that have it.
    """

    payload = extract_json_object(text)
    if payload is None:
        return None
    raw_dimensions = payload.get("dimensions")
    if not isinstance(raw_dimensions, list):
        return None

    dimensions: list[MatchDimension] = []
    for item in raw_dimensions:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        if name not in MATCH_DIMENSIONS:
            continue
        # A one-character evidence like "无" is a legitimate thing for the model
        # to write, but it trips MatchDimension's min_length and would drop the
        # whole dimension — which then fails the completeness check and loses the
        # entire report. Pad it while keeping what the model actually said.
        evidence = str(item.get("evidence", "")).strip()
        if len(evidence) < 2:
            evidence = f"未给出证据（原文：{evidence}）" if evidence else "未给出证据"
        try:
            dimensions.append(
                MatchDimension(
                    name=name,
                    # Clamp rather than reject: a 105 is a formatting slip, and
                    # losing the whole report over it serves nobody.
                    score=max(0, min(100, int(item.get("score", 0)))),
                    weight=max(0.01, min(1.0, float(item.get("weight", 0.2)))),
                    evidence=evidence[:600],
                    gap=str(item.get("gap", "")).strip()[:600],
                )
            )
        except (TypeError, ValueError):
            continue

    seen = {dimension.name for dimension in dimensions}
    if seen != set(MATCH_DIMENSIONS):
        return None

    verdict = str(payload.get("verdict", "")).strip()
    if verdict not in {"值得投", "补强后投", "不建议"}:
        verdict = "补强后投"
    blockers = [
        str(value).strip()
        for value in payload.get("blockers") or []
        if str(value).strip()
    ][:10]
    # A blocker and "值得投" contradict each other. The prompt forbids it; this
    # enforces it, because a score the user acts on must not say "apply" next to
    # "you are the wrong graduation year".
    if blockers and verdict == "值得投":
        verdict = "补强后投"

    try:
        report = MatchReport(
            company=str(payload.get("company", "")).strip()[:80] or "未标注",
            job_title=str(payload.get("job_title", "")).strip()[:120] or "未标注",
            job_url=str(payload.get("job_url", "")).strip()[:500],
            verdict=verdict,
            dimensions=dimensions,
            blockers=blockers,
            quick_wins=[
                str(value).strip()
                for value in payload.get("quick_wins") or []
                if str(value).strip()
            ][:10],
        )
    except ValueError:
        return None
    # The model supplies the parts; the total is arithmetic, so code owns it.
    return report.recomputed()


def render_match_report(report: MatchReport) -> str:
    """The report as Markdown, for Feishu and the chat window.

    The number leads, because that is what the user asked for, but every score
    is followed by its evidence — a bare 72 is exactly the kind of unsourced
    claim this project exists to prevent, and a digit disguises it better than
    a sentence would.
    """

    bar = "█" * round(report.overall / 10) + "░" * (10 - round(report.overall / 10))
    lines = [
        f"## 匹配度 {report.overall}/100 · {report.verdict}",
        f"`{bar}`  {report.company} · {report.job_title}",
    ]
    if report.job_url:
        lines.append(f"来源：{report.job_url}")
    if report.blockers:
        lines.append("")
        lines.append("**硬性阻断（总分已按此封顶）**")
        lines.extend(f"- ⛔ {blocker}" for blocker in report.blockers)
    lines.append("")
    lines.append("| 维度 | 分数 | 权重 | 依据 | 缺口 |")
    lines.append("|---|---:|---:|---|---|")
    for dimension in report.dimensions:
        lines.append(
            f"| {dimension.name} | {dimension.score} | "
            f"{dimension.weight:.0%} | {dimension.evidence} | "
            f"{dimension.gap or '—'} |"
        )
    if report.quick_wins:
        lines.append("")
        lines.append("**今天能提分的动作**")
        lines.extend(f"- {win}" for win in report.quick_wins)
    return "\n".join(lines)


#: Roles whose reply is JSON and has to be rendered before a human sees it.
#: Parallel to ``STRUCTURED_OUTPUT_SCHEMAS`` in ``anthropic_client``: one entry
#: says "constrain the output", this one says "render it back".
OUTPUT_RENDERERS = {
    "match_scorer": lambda text: (
        render_match_report(report)
        if (report := parse_match_report(text)) is not None
        else None
    ),
}


def render_structured_output(role_id: str, text: str) -> str:
    """Render a data role's JSON for humans; pass anything else through.

    Falls back to the raw text when parsing fails. Showing the user unparsed
    JSON is ugly, but showing them nothing loses a run they waited minutes for —
    and the raw reply is still the most useful thing available at that point.
    """

    renderer = OUTPUT_RENDERERS.get(role_id)
    if renderer is None:
        return text
    rendered = renderer(text)
    return rendered if rendered else text
