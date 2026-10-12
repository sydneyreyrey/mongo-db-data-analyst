"""Analyze mode: explore the company data with tools, then submit a structured report."""

import json
import time
from datetime import date
from typing import Any

from pydantic import BaseModel, ValidationError

from analyst.data import CompanyData, QueryRejected
from analyst.prompt_builder import render
from analyst.report import Report, chart_problems, sentences, vega_lite
from analyst.style import jargon
from analyst.tools import TOOLS, run_tool
from common import llm
from common.settings import get_settings
from harness.schema import Harness, Template
from harness.seed import has_custom_format

MAX_TURNS = 30
SANITY_CHECK = (
    "When a number jumps or drops sharply, check whether it is real before explaining it: look at who or what is "
    "behind it (the customers, accounts, or records involved), and watch for test or internal records, "
    "duplicates, and odd values. Look at actual examples, not just counts: sample a few of the records and group "
    "them by their parts (for example the domain of an email address, or the prefix of an ID or name)."
)
WRAP_UP_AT = 6  # turns left when the agent is told to submit
VALUE_TOLERANCE = 0.01  # relative difference allowed between a stated objective value and its pipeline


class Step(BaseModel):
    turn: int
    tool: str
    input: dict[str, Any]
    ok: bool
    output_preview: str
    seconds: float


class TemplateRun(BaseModel):
    id: str
    name: str
    ok: bool
    error: str = ""


class Analysis(BaseModel):
    report: Report
    charts: dict[str, dict]
    template_runs: list[TemplateRun]
    template_charts: dict[str, dict]
    steps: list[Step]
    usage: list[llm.Usage]
    turns: int

    @property
    def cost_usd(self) -> float:
        return round(sum(u.cost_usd for u in self.usage), 4)


class AnalysisFailed(RuntimeError):
    pass


async def run_templates(
    data: CompanyData, templates: list[Template]
) -> tuple[list[TemplateRun], dict[str, dict], list[str]]:
    """Run every template that is on. Returns run records, charts, and text the analyst reads."""
    runs, charts, texts = [], {}, []
    for t in (t for t in templates if t.status == "on"):
        try:
            rows = await data.aggregate(t.pipeline.collection, t.pipeline.stages)
            charts[t.id] = vega_lite(t.chart, rows)
            runs.append(TemplateRun(id=t.id, name=t.name, ok=True))
            texts.append(f"### {t.name} ({t.id})\n{t.description}\n{json.dumps(rows[:40], default=str)}")
        except Exception as e:  # noqa: BLE001  (a broken template is reported, not fatal)
            runs.append(TemplateRun(id=t.id, name=t.name, ok=False, error=str(e)[:300]))
            texts.append(f"### {t.name} ({t.id})\nFailed to run: {str(e)[:300]}")
    return runs, charts, texts


def kickoff(
    company_db: str,
    today: date,
    previous: list[str],
    standing_views: list[str],
    report_type: dict | None = None,
    custom_format: bool = False,
) -> str:
    kind = report_type or {"name": "General review", "brief": "Whatever matters most for the business right now."}
    parts = [
        f"Analyze the company database `{company_db}`. Today is {today.isoformat()}; you can only see data up to "
        f"today. This report is a {kind['name']}. Its purpose: {kind['brief']}",
        "Orient yourself (what collections exist, what a document looks like, the date range), then plan your "
        "investigation around that purpose: find what matters most for it right now (trends, drops, spikes, "
        "anomalies) and which segments drive them. Prioritize what changed recently. Compare against a baseline "
        "so every finding has a size.",
        "Check your numbers with queries before you claim them. " + SANITY_CHECK + " "
        "Report the status of every objective in the "
        "playbook. When done, call submit_report.",
        "Organize the report the way the playbook's 'Report format' section says: it decides what the focus is about, "
        "which sections come first, and what they are compared against. "
        "Write everything a person will read the way the playbook's 'How to write' section says. Give the report a "
        "short title that says what happened (never a generic label). Lead with the focus: the one thing they should "
        "pay attention to, as a headline, then at most three short sentences with the key numbers. Make every chart "
        "readable at a glance: a title that says what it shows, axis labels in plain words with units, money as "
        "currency, rates as percent, at most five colored series, and never a field name or code.",
    ]
    if custom_format:
        parts.append(
            "The reader has told you how they want reports organized (the playbook's 'Report format'). That format is "
            "the most important instruction here: it overrides the default structure and the report type's emphasis. "
            "Follow it exactly: the order of the story, which sections to include, and how many charts. Each item in "
            "`findings` is one section of that story, in order; a section has a chart only if the format allows it. "
            "In `format_followed`, list each instruction in the format and how this report follows it."
        )
    if previous:
        parts.append(f"Your previous {kind['name']} reported:\n" + "\n".join(f"- {t}" for t in previous))
    if standing_views:
        parts.append(
            "Standing views the user asked to see every time (already run, shown in the report as is). "
            "Comment on them in your summary if they show something important:\n\n" + "\n\n".join(standing_views)
        )
    return "\n\n".join(parts)


async def _validate_report(data: CompanyData, harness: Harness, args: dict) -> tuple[Report, dict[str, dict]]:
    """Parse the report and re-run every pipeline in it. Raises ValueError listing all problems."""
    try:
        report = Report.model_validate(args)
    except ValidationError as e:
        raise ValueError(f"Report does not match the schema: {e}") from e
    problems, charts = [], {}
    if report.title.strip().lower() in ("general review", "report", "business review", "review", ""):
        problems.append("title is generic; name the report after its main point")
    if not report.focus.headline.strip():
        problems.append("focus headline is empty")
    if sentences(report.focus.detail) > 3:
        problems.append(f"focus detail has {sentences(report.focus.detail)} sentences; use three or fewer")
    if has_custom_format(harness.output.form) and not report.format_followed:
        problems.append(
            "the reader set a report format; list each of its instructions in format_followed with how this report "
            "follows it, and make sure the sections really do"
        )
    if harness.preferences.writing_style == "simple":
        texts = {"title": report.title, "focus headline": report.focus.headline, "focus detail": report.focus.detail}
        texts |= {f"finding {f.id} title": f.title for f in report.findings}
        texts |= {f"finding {f.id} chart title": f.chart.title for f in report.findings if f.chart}
        for where, text in texts.items():
            if words := jargon(text):
                problems.append(f"{where} uses shorthand {words}; say it in plain everyday words")

    for f in report.findings:
        try:
            rows = await data.aggregate(f.pipeline.collection, f.pipeline.stages)
        except Exception as e:  # noqa: BLE001  (any failure is reported back to the model)
            problems.append(f"finding {f.id}: pipeline failed: {e}")
            continue
        if not rows:
            problems.append(f"finding {f.id}: pipeline returned no rows")
            continue
        if f.chart is None:
            continue
        missing = [c for c in (f.chart.x, f.chart.y, f.chart.color) if c and c not in rows[0]]
        if missing:
            problems.append(f"finding {f.id}: chart fields {missing} not in pipeline output keys {sorted(rows[0])}")
            continue
        unreadable = chart_problems(f.chart, rows)
        if unreadable:
            problems.extend(f"finding {f.id}: {p}" for p in unreadable)
            continue
        charts[f.id] = vega_lite(f.chart, rows)

    reported = {o.objective_id: o for o in report.objectives}
    for objective in harness.knowledge.objectives:
        status = reported.get(objective.id)
        if not status:
            problems.append(f"objective {objective.id}: missing status")
            continue
        try:
            rows = await data.aggregate(status.pipeline.collection, status.pipeline.stages)
        except Exception as e:  # noqa: BLE001
            problems.append(f"objective {objective.id}: pipeline failed: {e}")
            continue
        actual = rows[0].get("value") if rows else None
        if not isinstance(actual, int | float):
            problems.append(f"objective {objective.id}: pipeline must return a first row with a numeric `value`")
        elif abs(actual - status.value) > VALUE_TOLERANCE * max(abs(actual), 1e-9):
            problems.append(f"objective {objective.id}: stated value {status.value} but pipeline returns {actual}")

    if problems:
        raise ValueError("Fix these and call submit_report again:\n" + "\n".join(problems))
    return report, charts


async def analyze(
    harness: Harness,
    data: CompanyData,
    company_db: str,
    meter: llm.CostMeter,
    today: date,
    previous: list[str] | None = None,
    report_type: dict | None = None,
) -> Analysis:
    settings = get_settings()
    system = render(harness)
    template_runs, template_charts, standing = await run_templates(data, harness.knowledge.templates)
    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": kickoff(
                company_db, today, previous or [], standing, report_type, has_custom_format(harness.output.form)
            ),
        }
    ]
    steps: list[Step] = []
    usage: list[llm.Usage] = []

    for turn in range(1, MAX_TURNS + 1):
        response, u = await llm.call(
            "analyst",
            meter,
            model=settings.analyst_model,
            max_tokens=16000,
            output_config={"effort": "medium"},
            cache_control={"type": "ephemeral"},
            system=system,
            tools=TOOLS,
            messages=messages,
        )
        usage.append(u)
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "refusal":
            raise AnalysisFailed(f"Model refused: {getattr(response, 'stop_details', None)}")
        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            messages.append({"role": "user", "content": "Continue the analysis, then call submit_report."})
            continue

        results = []
        for block in tool_uses:
            start = time.perf_counter()
            ok = True
            if block.name == "submit_report":
                try:
                    report, charts = await _validate_report(data, harness, block.input)
                except ValueError as e:
                    ok, text = False, str(e)
                else:
                    seconds = round(time.perf_counter() - start, 3)
                    steps.append(Step(turn=turn, tool=block.name, input={}, ok=True, output_preview="accepted",
                                      seconds=seconds))  # fmt: skip
                    return Analysis(
                        report=report,
                        charts=charts,
                        template_runs=template_runs,
                        template_charts=template_charts,
                        steps=steps,
                        usage=usage,
                        turns=turn,
                    )
            else:
                try:
                    text = await run_tool(data, block.name, block.input)
                except (QueryRejected, KeyError, ValueError, TypeError) as e:
                    ok, text = False, f"{type(e).__name__}: {e}"
                except Exception as e:  # noqa: BLE001  (database errors go back to the model to fix)
                    ok, text = False, f"Query failed: {e}"
            seconds = round(time.perf_counter() - start, 3)
            preview = text[:300]
            steps.append(Step(turn=turn, tool=block.name, input=block.input, ok=ok, output_preview=preview,
                              seconds=seconds))  # fmt: skip
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": text, "is_error": not ok})

        remaining = MAX_TURNS - turn
        if remaining <= WRAP_UP_AT:
            results.append({"type": "text", "text": f"{remaining} turns left. Finish up and call submit_report."})
        messages.append({"role": "user", "content": results})

    raise AnalysisFailed(f"No valid report after {MAX_TURNS} turns")
