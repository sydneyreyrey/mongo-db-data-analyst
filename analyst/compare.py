"""Compare two reports: what the notebook learned between them, and how the findings changed.

Plain code, no model calls. Reports are ordered oldest first, whatever order they are passed in.
"""

from typing import Any


def _key(finding: dict) -> tuple:
    segment = tuple(sorted((k, str(v).lower()) for k, v in finding.get("segment", {}).items()))
    return (finding.get("metric", "").strip().lower(), segment)


def _brief(doc: dict) -> dict:
    return {
        "id": str(doc["_id"]),
        "as_of": doc.get("as_of"),
        "harness_version": doc["harness_version"],
        "created_at": doc["created_at"],
        "cost_usd": doc.get("cost_usd"),
        "summary": doc["report"]["summary"],
        "applied": doc["report"].get("applied", {}),
        "saved_views": [t["name"] for t in doc.get("template_runs", []) if t["ok"]],
    }


def _finding(f: dict) -> dict:
    return {
        k: f.get(k) for k in ("id", "title", "metric", "segment", "period", "direction", "magnitude", "dollar_impact")
    }


def match_findings(old: list[dict], new: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """Pair findings by id, then by the same metric on the same segment. Returns (both, only_new, only_old)."""
    both, remaining = [], list(new)
    unmatched_old = []
    for f in old:
        hit = next((g for g in remaining if g["id"] == f["id"]), None) or next(
            (g for g in remaining if _key(g) == _key(f)), None
        )
        if hit:
            remaining.remove(hit)
            both.append({"before": _finding(f), "after": _finding(hit)})
        else:
            unmatched_old.append(_finding(f))
    return both, [_finding(g) for g in remaining], unmatched_old


def compare(a: dict, b: dict, versions: list[dict]) -> dict[str, Any]:
    """`versions` is the workspace's harness history (store.history)."""
    old, new = sorted([a, b], key=lambda d: d["created_at"])
    lo, hi = sorted([old["harness_version"], new["harness_version"]])
    changes = [
        {"version": v["version"], "created_by": v["created_by"], "created_at": v["created_at"], "edits": v["edits"]}
        for v in versions
        if lo < v["version"] <= hi
    ]
    both, appeared, disappeared = match_findings(old["report"]["findings"], new["report"]["findings"])
    goals_old = {o["objective_id"]: o for o in old["report"].get("objectives", [])}
    goals_new = {o["objective_id"]: o for o in new["report"].get("objectives", [])}
    goals = [
        {
            "objective_id": gid,
            "before": {k: goals_old[gid][k] for k in ("value", "target", "status")} if gid in goals_old else None,
            "after": {k: goals_new[gid][k] for k in ("value", "target", "status")} if gid in goals_new else None,
        }
        for gid in sorted(goals_old.keys() | goals_new.keys())
    ]
    same_date = old.get("as_of") == new.get("as_of")
    return {
        "before": _brief(old),
        "after": _brief(new),
        "same_review_date": same_date,
        "note": (
            "Same review date: differences come from what the analyst learned (and normal run to run variation)."
            if same_date
            else "Different review dates: differences mix new data with what the analyst learned."
        ),
        "notebook_changes": changes,
        "findings": {"in_both": both, "new": appeared, "dropped": disappeared},
        "objectives": goals,
    }
