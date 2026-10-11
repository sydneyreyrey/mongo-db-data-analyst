"""The structured report the analyst submits, and chart building from real query results."""

import re
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


class Focus(BaseModel):
    headline: str = Field(description="The one thing the reader should focus on, as a plain sentence")
    detail: str = Field(description="Why, in 3 sentences or fewer, with the key numbers")


class Report(BaseModel):
    title: str = Field(description="A concrete name for this report that states its main point, not a generic label")
    focus: Focus
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


FORMATS = {"number": ",.0f", "currency": "$,.0f", "percent": ".0%"}
MAX_SERIES = 5


def humanize(field: str) -> str:
    return field.strip("_").replace("_", " ").replace(".", " ").strip().capitalize()


def sentences(text: str) -> int:
    return len([s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s])


def chart_problems(chart: Chart, rows: list[dict] | None = None) -> list[str]:
    """Rules that keep charts readable by anyone. Empty list means the chart is fine."""
    problems = []
    for name in ("title", "x_label", "y_label"):
        value = getattr(chart, name).strip()
        if not value:
            problems.append(f"chart {name} is required, in plain words")
        elif "_" in value or "$$" in value or value in (chart.x, chart.y):
            problems.append(f"chart {name} {value!r} looks like a field name; use plain words")
    if chart.color and not chart.color_label.strip():
        problems.append("chart color_label is required when color is used")
    if len(chart.title) > 90:
        problems.append("chart title is too long; keep it under 90 characters")
    if rows and chart.color:
        series = {r.get(chart.color) for r in rows}
        if len(series) > MAX_SERIES:
            problems.append(f"chart has {len(series)} series; show at most {MAX_SERIES} (group the rest as Other)")
    return problems


def vega_lite(chart: Chart, rows: list[dict]) -> dict:
    """Build a Vega-Lite spec whose data is the finding's own pipeline output, labeled in plain words."""
    rows = [{k: v for k, v in r.items() if k in {chart.x, chart.y, chart.color}} for r in rows]
    x_type = _vl_type([r.get(chart.x) for r in rows])
    x_title = chart.x_label or humanize(chart.x)
    y_title = chart.y_label or humanize(chart.y)
    y_format = FORMATS[chart.y_format]
    x: dict[str, Any] = {"field": chart.x, "type": x_type, "title": x_title}
    if x_type == "temporal":
        x["axis"] = {"format": "%b %d, %Y" if len(rows) > 24 else "%b %Y", "labelAngle": 0}
    elif x_type == "nominal" and chart.type == "bar":
        x["sort"] = "-y"  # biggest first
        x["axis"] = {"labelAngle": 0}
    tooltip = [
        {"field": chart.x, "title": x_title, **({"format": "%b %d, %Y"} if x_type == "temporal" else {})},
        {"field": chart.y, "title": y_title, "format": y_format},
    ]
    encoding: dict[str, Any] = {
        "x": x,
        "y": {"field": chart.y, "type": "quantitative", "title": y_title, "axis": {"format": y_format}},
        "tooltip": tooltip,
    }
    if chart.color:
        color_title = chart.color_label or humanize(chart.color)
        encoding["color"] = {"field": chart.color, "type": "nominal", "title": color_title}
        tooltip.insert(1, {"field": chart.color, "title": color_title})
    return {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "title": {"text": chart.title or y_title, "anchor": "start"},
        "width": "container",
        "height": 240,
        "data": {"values": rows},
        "mark": {"type": chart.type, "tooltip": True, **({"point": True} if chart.type == "line" else {})},
        "encoding": encoding,
        "config": {"axis": {"grid": True, "tickCount": 5}, "view": {"stroke": None}},
    }
