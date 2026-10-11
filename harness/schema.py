"""The Harness: everything the analyst knows about one company, as versioned data.

Field declaration order is prompt order. Every learned entry records its `source`: the comment or
objective id that caused it.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field

MAX_TIME_MS_CEILING = 10_000
TimeGrain = Literal["day", "week", "month", "quarter"]

# Vetted tools Reflect may enable per company. Implementations live in analyst/shelf.py.
SHELF_TOOLS = ("forecast", "detect_spikes", "compare_same_period_last_year", "holiday_calendar", "web_search")


class Pipeline(BaseModel):
    collection: str
    stages: list[dict[str, Any]] = Field(
        description="Aggregation stages. Exclusions and the as-of date are applied automatically. "
        'Dates as {"$date": "2024-01-01T00:00:00Z"}.'
    )


class Chart(BaseModel):
    """A chart for a non-technical reader. Field names stay internal; people only see the labels."""

    type: Literal["line", "bar", "area", "point"]
    x: str = Field(description="Field in the pipeline output rows for the x axis")
    y: str = Field(description="Field in the pipeline output rows for the y axis")
    color: str | None = Field(default=None, description="Optional field to split series by (at most 5 series)")
    title: str = Field(default="", description="Plain words stating the takeaway, e.g. 'Signups doubled in March'")
    x_label: str = Field(default="", description="Plain words for the x axis, e.g. 'Region' or 'Week'")
    y_label: str = Field(default="", description="Plain words with units for the y axis, e.g. 'Profit ($)'")
    color_label: str = Field(default="", description="Plain words for the legend, if color is used")
    y_format: Literal["number", "currency", "percent"] = Field(
        default="number", description="How to show y values; percent expects fractions (0.45 shows as 45%)"
    )


class Identity(BaseModel):
    role: str = ""
    audience: str = ""
    values: list[str] = []


class Term(BaseModel):
    definition: str
    source: str = ""


class Rule(BaseModel):
    id: str
    text: str
    source: str = ""


class FocusMetric(BaseModel):
    name: str
    definition: str
    source: str = ""


class Exclusion(BaseModel):
    id: str
    filter: dict[str, Any]
    reason: str
    source: str = ""


class Objective(BaseModel):
    id: str
    text: str
    metric: str = Field(description="How the value is computed, precisely enough to reproduce")
    target: float
    direction: Literal["at_least", "at_most"]
    deadline: str
    source: str = ""


class Template(BaseModel):
    id: str
    name: str
    description: str
    pipeline: Pipeline
    chart: Chart
    status: Literal["on", "off"] = "on"
    source: str = ""


class Knowledge(BaseModel):
    collections: dict[str, str] = {}
    vocabulary: dict[str, Term] = {}
    rules: list[Rule] = []
    focus_metrics: list[FocusMetric] = []
    exclusions: list[Exclusion] = []
    objectives: list[Objective] = []
    templates: list[Template] = []


class Capabilities(BaseModel):
    tools_enabled: list[str] = []


class Preferences(BaseModel):
    time_grain: TimeGrain = "month"
    max_findings: int = Field(default=5, ge=1, le=10)
    chart_style: str = ""


class Constraints(BaseModel):
    max_docs: int = Field(default=1000, ge=1, le=5000)
    max_time_ms: int = Field(default=MAX_TIME_MS_CEILING, ge=100, le=MAX_TIME_MS_CEILING)


class Policy(BaseModel):
    scope: str = ""
    constraints: Constraints = Constraints()


class Output(BaseModel):
    form: str = ""


class Harness(BaseModel):
    workspace_id: str
    version: int = 1
    parent_version: int | None = None
    identity: Identity = Identity()
    knowledge: Knowledge = Knowledge()
    capabilities: Capabilities = Capabilities()
    preferences: Preferences = Preferences()
    policy: Policy = Policy()
    output: Output = Output()
