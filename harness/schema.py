"""The Harness: everything the analyst knows about one company, as versioned data.

Field declaration order is prompt order.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field

MAX_TIME_MS_CEILING = 10_000
TimeGrain = Literal["day", "week", "month", "quarter"]


class Identity(BaseModel):
    role: str = ""
    audience: str = ""
    values: list[str] = []


class Term(BaseModel):
    definition: str
    source_comment: str = ""


class Rule(BaseModel):
    id: str
    text: str
    source_comment: str = ""


class FocusMetric(BaseModel):
    name: str
    definition: str
    source_comment: str = ""


class Exclusion(BaseModel):
    id: str
    filter: dict[str, Any]
    reason: str
    source_comment: str = ""


class Knowledge(BaseModel):
    collections: dict[str, str] = {}
    vocabulary: dict[str, Term] = {}
    rules: list[Rule] = []
    focus_metrics: list[FocusMetric] = []
    exclusions: list[Exclusion] = []


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
    preferences: Preferences = Preferences()
    policy: Policy = Policy()
    output: Output = Output()
