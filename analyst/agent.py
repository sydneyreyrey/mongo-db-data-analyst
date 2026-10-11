"""Analyze mode: explore the company data with tools, then submit a structured report."""

import time
from datetime import date
from typing import Any

from pydantic import BaseModel, ValidationError

from analyst.data import CompanyData, QueryRejected
from analyst.prompt_builder import render
from analyst.report import Report, vega_lite
from analyst.tools import TOOLS, run_tool
from common import llm
from common.settings import get_settings
from harness.schema import Harness

MAX_TURNS = 30
WRAP_UP_AT = 6  # turns left when the agent is told to submit


class Step(BaseModel):
    turn: int
    tool: str
    input: dict[str, Any]
    ok: bool
    output_preview: str
    seconds: float


class Analysis(BaseModel):
    report: Report
    charts: dict[str, dict]
    steps: list[Step]
    usage: list[llm.Usage]
    turns: int

    @property
    def cost_usd(self) -> float:
        return round(sum(u.cost_usd for u in self.usage), 4)


class AnalysisFailed(RuntimeError):
    pass


def kickoff(company_db: str, today: date) -> str:
    return (
        f"Analyze the company database `{company_db}`. Today is {today.isoformat()}.\n"
        "Orient yourself first (what collections exist, what a document looks like, the date range), "
        "then investigate what is most important for this business: trends, drops, spikes, anomalies, "
        "and which segments drive them. Compare against a baseline so every finding has a size.\n"
        "Check your numbers with queries before you claim them. When done, call submit_report."
    )


async def _validate_report(data: CompanyData, args: dict) -> tuple[Report, dict[str, dict]]:
    """Parse the report and re-run every finding's pipeline. Raises ValueError with all problems."""
    try:
        report = Report.model_validate(args)
    except ValidationError as e:
        raise ValueError(f"Report does not match the schema: {e}") from e
    problems, charts = [], {}
    for f in report.findings:
        try:
            rows = await data.aggregate(f.pipeline.collection, f.pipeline.stages)
        except Exception as e:  # noqa: BLE001  (any failure is reported back to the model)
            problems.append(f"{f.id}: pipeline failed: {e}")
            continue
        if not rows:
            problems.append(f"{f.id}: pipeline returned no rows")
            continue
        missing = [c for c in (f.chart.x, f.chart.y, f.chart.color) if c and c not in rows[0]]
        if missing:
            problems.append(f"{f.id}: chart fields {missing} not in pipeline output keys {sorted(rows[0])}")
            continue
        charts[f.id] = vega_lite(f.chart, rows)
    if problems:
        raise ValueError("Fix these and call submit_report again:\n" + "\n".join(problems))
    return report, charts


async def analyze(
    harness: Harness, data: CompanyData, company_db: str, meter: llm.CostMeter, today: date | None = None
) -> Analysis:
    settings = get_settings()
    system = render(harness)
    messages: list[dict[str, Any]] = [{"role": "user", "content": kickoff(company_db, today or date.today())}]
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
                    report, charts = await _validate_report(data, block.input)
                except ValueError as e:
                    ok, text = False, str(e)
                else:
                    steps.append(
                        Step(
                            turn=turn,
                            tool=block.name,
                            input={},
                            ok=True,
                            output_preview="accepted",
                            seconds=round(time.perf_counter() - start, 3),
                        )
                    )
                    return Analysis(report=report, charts=charts, steps=steps, usage=usage, turns=turn)
            else:
                try:
                    text = await run_tool(data, block.name, block.input)
                except (QueryRejected, KeyError, ValueError, TypeError) as e:
                    ok, text = False, f"{type(e).__name__}: {e}"
                except Exception as e:  # noqa: BLE001  (database errors go back to the model to fix)
                    ok, text = False, f"Query failed: {e}"
            steps.append(
                Step(
                    turn=turn,
                    tool=block.name,
                    input=block.input,
                    ok=ok,
                    output_preview=text[:300],
                    seconds=round(time.perf_counter() - start, 3),
                )
            )
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": text, "is_error": not ok})

        remaining = MAX_TURNS - turn
        if remaining <= WRAP_UP_AT:
            results.append({"type": "text", "text": f"{remaining} turns left. Finish up and call submit_report."})
        messages.append({"role": "user", "content": results})

    raise AnalysisFailed(f"No valid report after {MAX_TURNS} turns")
