"""Typed harness edits and the applier. This is the only way a harness changes."""

from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from harness.locked import LockedFieldError, assert_locked_unchanged, is_locked
from harness.schema import (
    SHELF_TOOLS,
    Chart,
    Exclusion,
    FocusMetric,
    Harness,
    Objective,
    Pipeline,
    Preferences,
    Rule,
    Template,
    Term,
)

Action = Literal[
    "annotate_collection",
    "set_vocab",
    "remove_vocab",
    "add_rule",
    "retire_rule",
    "set_focus_metric",
    "remove_focus_metric",
    "add_exclusion",
    "remove_exclusion",
    "set_preference",
    "set_objective",
    "retire_objective",
    "save_template",
    "set_template_status",
    "enable_tool",
    "disable_tool",
    "set_report_format",
]

# Operators a stored filter or pipeline may not contain (they write, run code, or reach other data).
FORBIDDEN_OPERATORS = {"$where", "$function", "$accumulator", "$lookup", "$unionWith", "$graphLookup", "$out", "$merge"}


class Edit(BaseModel):
    action: Action
    target: str = Field(min_length=1, description="Id or name of the entry being changed")
    payload: dict[str, Any] = {}
    rationale: str = Field(min_length=1)
    source: str = Field(min_length=1, description="The comment id or objective id that motivated this edit")


class InvalidEdit(ValueError):
    pass


def _walk_keys(value: Any):
    if isinstance(value, dict):
        for k, v in value.items():
            yield k
            yield from _walk_keys(v)
    elif isinstance(value, list):
        for v in value:
            yield from _walk_keys(v)


def _require(payload: dict[str, Any], key: str) -> Any:
    if key not in payload or payload[key] in ("", None):
        raise InvalidEdit(f"payload.{key} is required")
    return payload[key]


def _safe(value: Any, what: str) -> None:
    bad = FORBIDDEN_OPERATORS.intersection(_walk_keys(value))
    if bad:
        raise InvalidEdit(f"{what} may not use {sorted(bad)}")


def _build(model: type[BaseModel], data: dict[str, Any], what: str) -> Any:
    try:
        return model.model_validate(data)
    except ValidationError as e:
        err = e.errors()[0]
        raise InvalidEdit(f"Invalid {what}: {'.'.join(map(str, err['loc']))}: {err['msg']}") from e


def _find(items: list, key: str, value: str) -> Any:
    return next((i for i in items if getattr(i, key) == value), None)


def apply_edit(harness: Harness, edit: Edit) -> Harness:
    """Return a new harness version with `edit` applied. Never mutates the input."""
    if is_locked(edit.target):
        raise LockedFieldError(f"{edit.target!r} is locked")

    new = harness.model_copy(deep=True)
    k, t, p, src = new.knowledge, edit.target, edit.payload, edit.source

    match edit.action:
        case "annotate_collection":
            k.collections[t] = _require(p, "note")
        case "set_vocab":
            k.vocabulary[t] = Term(definition=_require(p, "definition"), source=src)
        case "remove_vocab":
            if k.vocabulary.pop(t, None) is None:
                raise InvalidEdit(f"No vocabulary term {t!r}")
        case "add_rule":
            if _find(k.rules, "id", t):
                raise InvalidEdit(f"Rule {t!r} already exists")
            k.rules.append(Rule(id=t, text=_require(p, "text"), source=src))
        case "retire_rule":
            if not _find(k.rules, "id", t):
                raise InvalidEdit(f"No rule {t!r}")
            k.rules = [r for r in k.rules if r.id != t]
        case "set_focus_metric":
            metric = FocusMetric(name=t, definition=_require(p, "definition"), source=src)
            k.focus_metrics = [m for m in k.focus_metrics if m.name != t] + [metric]
        case "remove_focus_metric":
            if not _find(k.focus_metrics, "name", t):
                raise InvalidEdit(f"No focus metric {t!r}")
            k.focus_metrics = [m for m in k.focus_metrics if m.name != t]
        case "add_exclusion":
            flt = _require(p, "filter")
            if not isinstance(flt, dict):
                raise InvalidEdit("payload.filter must be a MongoDB filter object")
            _safe(flt, "Exclusion filter")
            if _find(k.exclusions, "id", t):
                raise InvalidEdit(f"Exclusion {t!r} already exists")
            k.exclusions.append(Exclusion(id=t, filter=flt, reason=_require(p, "reason"), source=src))
        case "remove_exclusion":
            if not _find(k.exclusions, "id", t):
                raise InvalidEdit(f"No exclusion {t!r}")
            k.exclusions = [e for e in k.exclusions if e.id != t]
        case "set_preference":
            if t not in Preferences.model_fields:
                raise InvalidEdit(f"Unknown preference {t!r}")
            new.preferences = _build(Preferences, {**new.preferences.model_dump(), t: _require(p, "value")}, t)
        case "set_objective":
            objective = _build(Objective, {**p, "id": t, "source": src}, "objective")
            k.objectives = [o for o in k.objectives if o.id != t] + [objective]
        case "retire_objective":
            if not _find(k.objectives, "id", t):
                raise InvalidEdit(f"No objective {t!r}")
            k.objectives = [o for o in k.objectives if o.id != t]
        case "save_template":
            _build(Pipeline, _require(p, "pipeline"), "template pipeline")
            _build(Chart, _require(p, "chart"), "template chart")
            _safe(p["pipeline"], "Template pipeline")
            template = _build(Template, {**p, "id": t, "status": "on", "source": src}, "template")
            k.templates = [x for x in k.templates if x.id != t] + [template]
        case "set_template_status":
            template = _find(k.templates, "id", t)
            if not template:
                raise InvalidEdit(f"No template {t!r}")
            status = _require(p, "status")
            if status not in ("on", "off"):
                raise InvalidEdit("payload.status must be 'on' or 'off'")
            template.status = status
            template.source = src
        case "enable_tool":
            if t not in SHELF_TOOLS:
                raise InvalidEdit(f"{t!r} is not on the tool shelf {list(SHELF_TOOLS)}")
            if t not in new.capabilities.tools_enabled:
                new.capabilities.tools_enabled.append(t)
        case "set_report_format":
            if t != "report":
                raise InvalidEdit("set_report_format target must be 'report'")
            new.output.form = _require(p, "format")
        case "disable_tool":
            if t not in new.capabilities.tools_enabled:
                raise InvalidEdit(f"Tool {t!r} is not enabled")
            new.capabilities.tools_enabled.remove(t)

    new.parent_version = harness.version
    new.version = harness.version + 1
    assert_locked_unchanged(harness, new)
    return new
