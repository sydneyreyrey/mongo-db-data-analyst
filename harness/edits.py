"""Typed harness edits and the applier. This is the only way a harness changes."""

from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from harness.locked import LockedFieldError, assert_locked_unchanged, is_locked
from harness.schema import Exclusion, FocusMetric, Harness, Preferences, Rule, Term

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
]

# Operators an exclusion filter may not contain (they run code or reach other data).
FORBIDDEN_OPERATORS = {"$where", "$function", "$accumulator", "$lookup", "$unionWith"}


class Edit(BaseModel):
    action: Action
    target: str = Field(min_length=1)
    payload: dict[str, Any] = {}
    rationale: str = Field(min_length=1)
    comment_id: str = Field(min_length=1)


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


def apply_edit(harness: Harness, edit: Edit) -> Harness:
    """Return a new harness version with `edit` applied. Never mutates the input."""
    if is_locked(edit.target):
        raise LockedFieldError(f"{edit.target!r} is locked")

    new = harness.model_copy(deep=True)
    k, t, p, src = new.knowledge, edit.target, edit.payload, edit.comment_id

    match edit.action:
        case "annotate_collection":
            k.collections[t] = _require(p, "note")
        case "set_vocab":
            k.vocabulary[t] = Term(definition=_require(p, "definition"), source_comment=src)
        case "remove_vocab":
            if k.vocabulary.pop(t, None) is None:
                raise InvalidEdit(f"No vocabulary term {t!r}")
        case "add_rule":
            if any(r.id == t for r in k.rules):
                raise InvalidEdit(f"Rule {t!r} already exists")
            k.rules.append(Rule(id=t, text=_require(p, "text"), source_comment=src))
        case "retire_rule":
            if not any(r.id == t for r in k.rules):
                raise InvalidEdit(f"No rule {t!r}")
            k.rules = [r for r in k.rules if r.id != t]
        case "set_focus_metric":
            metric = FocusMetric(name=t, definition=_require(p, "definition"), source_comment=src)
            k.focus_metrics = [m for m in k.focus_metrics if m.name != t] + [metric]
        case "remove_focus_metric":
            if not any(m.name == t for m in k.focus_metrics):
                raise InvalidEdit(f"No focus metric {t!r}")
            k.focus_metrics = [m for m in k.focus_metrics if m.name != t]
        case "add_exclusion":
            flt = _require(p, "filter")
            if not isinstance(flt, dict):
                raise InvalidEdit("payload.filter must be a MongoDB filter object")
            bad = FORBIDDEN_OPERATORS.intersection(_walk_keys(flt))
            if bad:
                raise InvalidEdit(f"Exclusion filter may not use {sorted(bad)}")
            if any(e.id == t for e in k.exclusions):
                raise InvalidEdit(f"Exclusion {t!r} already exists")
            reason = _require(p, "reason")
            k.exclusions.append(Exclusion(id=t, filter=flt, reason=reason, source_comment=src))
        case "remove_exclusion":
            if not any(e.id == t for e in k.exclusions):
                raise InvalidEdit(f"No exclusion {t!r}")
            k.exclusions = [e for e in k.exclusions if e.id != t]
        case "set_preference":
            if t not in Preferences.model_fields:
                raise InvalidEdit(f"Unknown preference {t!r}")
            try:
                new.preferences = Preferences.model_validate({**new.preferences.model_dump(), t: _require(p, "value")})
            except ValidationError as e:
                raise InvalidEdit(f"Invalid value for {t!r}: {e.errors()[0]['msg']}") from e

    new.parent_version = harness.version
    new.version = harness.version + 1
    assert_locked_unchanged(harness, new)
    return new
