"""Human readable diff between two harness versions."""

import json
from typing import Any

from harness.schema import Harness

SKIP = {"version", "parent_version"}
# Maps whose entries are shown whole (one line per term) rather than field by field.
WHOLE_ENTRIES = {"knowledge.vocabulary"}


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    """Flatten to dotted paths. Lists of objects with an id or name are keyed by it.

    Empty containers produce no entries, so adding the first item reads as one "+" line.
    """
    if isinstance(value, dict | list) and not value:
        return {}
    if isinstance(value, dict) and prefix in WHOLE_ENTRIES:
        return {f"{prefix}.{k}": v for k, v in value.items()}
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            out.update(_flatten(v, f"{prefix}.{k}" if prefix else k))
        return out
    if isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
        out = {}
        for i, v in enumerate(value):
            key = v.get("id") or v.get("name") or str(i)
            out[f"{prefix}[{key}]"] = v
        return out
    return {prefix: value}


def _fmt(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)


def diff(old: Harness, new: Harness) -> list[str]:
    a = {k: v for k, v in _flatten(old.model_dump()).items() if k not in SKIP}
    b = {k: v for k, v in _flatten(new.model_dump()).items() if k not in SKIP}
    lines = []
    for path in sorted(a.keys() | b.keys()):
        if path not in a:
            lines.append(f"+ {path}: {_fmt(b[path])}")
        elif path not in b:
            lines.append(f"- {path}: {_fmt(a[path])}")
        elif a[path] != b[path]:
            lines.append(f"~ {path}: {_fmt(a[path])} -> {_fmt(b[path])}")
    return lines
