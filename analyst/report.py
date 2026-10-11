"""The structured report the analyst submits, and chart building from real query results."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from harness.schema import Chart, Pipeline


class Period(BaseModel):
    start: str = Field(description="ISO date, e.g. 2024-04-01")
    end: str = Field(description="ISO date, inclusive")


class Finding(BaseModel):
    id: str = Field(description="Short slug, e.g. west_region_revenue_drop")
    title: str
    why_it_matters: str
    metric: str = Field(description="What is measured, e.g. revenue, margin, rating, signups")
    segment: dict[str, Any] = Field(description='Where it happens, e.g. {"region": "West"}; {} if overall')
    period: Period
    direction: Literal["up", "down", "anomaly"]
    magnitude: float = Field(description="Relative change vs the comparison baseline, e.g. -0.45")
    dollar_impact: float | None = Field(default=None, description="Estimated dollars at stake, if known")
    pipeline: Pipeline = Field(description="Pipeline whose output proves the finding and feeds the chart")
    chart: Chart


class ObjectiveStatus(BaseModel):
    objective_id: str
    value: float = Field(description="Current value of the objective's metric, as defined in the playbook")
    target: float
    status: Literal["on_track", "off_track", "met", "missed"]
    note: str = Field(description="One sentence on what is driving it")
    pipeline: Pipeline = Field(description="Pipeline whose first output row has a field named `value`")


class Applied(BaseModel):
    focus_metrics: list[str] = []
    time_grain: str = ""


class Report(BaseModel):
    summary: str
    findings: list[Finding]
    objectives: list[ObjectiveStatus] = Field(default=[], description="One entry per active objective")
    applied: Applied


def _vl_type(values: list[Any]) -> str:
    sample = [v for v in values if v is not None][:20]
    if sample and all(isinstance(v, int | float) and not isinstance(v, bool) for v in sample):
        return "quantitative"
    if sample and all(isinstance(v, str) for v in sample):
        try:
            for v in sample:
                datetime.fromisoformat(v)
            return "temporal"
        except ValueError:
            pass
    return "nominal"


def vega_lite(chart: Chart, rows: list[dict]) -> dict:
    """Build a Vega-Lite spec whose data is the finding's own pipeline output."""
    rows = [{k: v for k, v in r.items() if k in {chart.x, chart.y, chart.color}} for r in rows]
    x_type = _vl_type([r.get(chart.x) for r in rows])
    encoding: dict[str, Any] = {
        "x": {"field": chart.x, "type": x_type},
        "y": {"field": chart.y, "type": "quantitative"},
        "tooltip": [{"field": f} for f in (chart.x, chart.y, chart.color) if f],
    }
    if chart.color:
        encoding["color"] = {"field": chart.color, "type": "nominal"}
    return {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "title": chart.title,
        "width": "container",
        "height": 240,
        "data": {"values": rows},
        "mark": {"type": chart.type, "tooltip": True, **({"point": True} if chart.type == "line" else {})},
        "encoding": encoding,
    }
