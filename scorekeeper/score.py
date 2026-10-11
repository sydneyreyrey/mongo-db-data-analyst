"""Score one quarterly report against the answer key. Plain code only: no LLM calls in this package.

Four parts, each 0 to 1, averaged into a total (parts with nothing to score are left out):
  recall           planted stories active this quarter that a finding matches
  compliance       earlier manager comments whose machine check passes
  objectives       active objectives whose reported value matches the reference within tolerance
  reproducibility  findings whose pipeline re-runs and returns rows
"""

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from bson import json_util
from pydantic import BaseModel
from pymongo.asynchronous.database import AsyncDatabase

from analyst.data import CompanyData
from harness.schema import Harness

BENCH = Path(__file__).resolve().parent.parent / "bench"


class Check(BaseModel):
    id: str
    type: str
    passed: bool
    detail: str = ""


class Part(BaseModel):
    value: float | None
    checks: list[Check] = []


class Scorecard(BaseModel):
    company: str
    cycle: int
    as_of: str
    harness_version: int
    recall: Part
    compliance: Part
    objectives: Part
    reproducibility: Part
    total: float
    other_findings: int


def load_key(company: str) -> tuple[list[dict], list[dict]]:
    planted = json.loads((BENCH / company / "planted.json").read_text())["findings"]
    cycles = json.loads((BENCH / company / "timeline.json").read_text())["cycles"]
    return planted, cycles


def quarter_start(day: date) -> date:
    return date(day.year, 3 * ((day.month - 1) // 3) + 1, 1)


def _date(text: str) -> date | None:
    try:
        return date.fromisoformat(str(text)[:10])
    except ValueError:
        return None


def _overlaps(a_start: date, a_end: date, b_start: date, b_end: date) -> bool:
    return a_start <= b_end and b_start <= a_end


def _text(finding: dict) -> str:
    return " ".join(
        [json.dumps(finding.get("segment", {})), finding.get("title", ""), finding.get("why_it_matters", "")]
    ).lower()


def matches(finding: dict, story: dict, check_period: bool = True) -> bool:
    """Same kind of metric, same direction, the story's segment named, and (optionally) overlapping time."""
    rule = story["match"]
    metric_text = f"{finding.get('metric', '')} {finding.get('title', '')}".lower()
    if not any(m in metric_text for m in rule["metrics"]):
        return False
    if finding.get("direction") not in rule["directions"]:
        return False
    text = _text(finding)
    if not all(any(term in text for term in group) for group in rule["terms"]):
        return False
    if check_period:
        f_start, f_end = _date(finding["period"]["start"]), _date(finding["period"]["end"])
        if not f_start or not f_end:
            return False
        s_start, s_end = date.fromisoformat(story["period"]["start"]), date.fromisoformat(story["period"]["end"])
        if not _overlaps(f_start, f_end, s_start, s_end):
            return False
    return True


def _nested(field: str, value: Any) -> dict:
    doc: Any = value
    for part in reversed(field.split(".")):
        doc = {part: doc}
    return doc


async def excludes(db: AsyncDatabase, harness: Harness, sample: dict) -> bool:
    """Would the harness exclusions drop a document like `sample`?"""
    filters = [json_util.loads(json_util.dumps(e.filter)) for e in harness.knowledge.exclusions]
    if not filters:
        return False
    cursor = await db.aggregate([{"$documents": [sample]}, {"$match": {"$or": filters}}])
    return bool(await cursor.to_list())


def _fill(value: Any, slots: dict[str, datetime]) -> Any:
    if isinstance(value, str) and value in slots:
        return slots[value]
    if isinstance(value, dict):
        return {k: _fill(v, slots) for k, v in value.items()}
    if isinstance(value, list):
        return [_fill(v, slots) for v in value]
    return value


async def reference_value(db: AsyncDatabase, reference: dict, as_of: date) -> float | None:
    slots = {
        "$$AS_OF": datetime.combine(as_of, datetime.max.time()),
        "$$QUARTER_START": datetime.combine(quarter_start(as_of), datetime.min.time()),
        "$$YEAR_START": datetime(as_of.year, 1, 1),
    }
    cursor = await db[reference["collection"]].aggregate(_fill(reference["stages"], slots))
    rows = await cursor.to_list()
    return rows[0]["value"] if rows else None


def _part(checks: list[Check]) -> Part:
    return Part(value=round(sum(c.passed for c in checks) / len(checks), 4) if checks else None, checks=checks)


async def score_report(
    db: AsyncDatabase,
    company: str,
    report_doc: dict,
    harness: Harness,
    cycle: int,
    time_fields: dict[str, str],
) -> Scorecard:
    planted, cycles = load_key(company)
    as_of = date.fromisoformat(report_doc["as_of"])
    window = (quarter_start(as_of), as_of)
    findings = report_doc["report"]["findings"]
    earlier = [c for i, cyc in enumerate(cycles) if i < cycle for c in cyc["comments"]]
    suppressed = {c["check"]["story_id"] for c in earlier if c["check"] and c["check"]["type"] == "suppress_story"}

    # Recall: stories active this quarter and not suppressed by the manager.
    recall: list[Check] = []
    matched_findings: set[int] = set()
    for story in planted:
        s_start, s_end = date.fromisoformat(story["period"]["start"]), date.fromisoformat(story["period"]["end"])
        if story["id"] in suppressed or not _overlaps(s_start, s_end, *window):
            continue
        hits = [i for i, f in enumerate(findings) if matches(f, story)]
        matched_findings.update(hits)
        resolved = story["match"].get("resolved_by_exclusion")
        if hits:
            recall.append(Check(id=story["id"], type="story", passed=True, detail=f"finding {findings[hits[0]]['id']}"))
        elif resolved and await excludes(db, harness, resolved):
            recall.append(Check(id=story["id"], type="story", passed=True, detail="resolved by exclusion"))
        else:
            recall.append(Check(id=story["id"], type="story", passed=False))

    # Compliance: every earlier comment with a machine check (objectives are scored separately).
    compliance: list[Check] = []
    stories = {s["id"]: s for s in planted}
    template_runs = {t["id"] for t in report_doc.get("template_runs", []) if t["ok"]}
    for c in earlier:
        check = c["check"]
        if not check or check["type"] == "objective":
            continue
        kind, passed, detail = check["type"], False, ""
        if kind == "focus_metric":
            kw = check["keyword"]
            in_harness = any(kw in f"{m.name} {m.definition}".lower() for m in harness.knowledge.focus_metrics)
            share = sum(kw in f"{f['metric']} {f['title']}".lower() for f in findings) / max(len(findings), 1)
            passed, detail = in_harness and share >= 0.5, f"in harness={in_harness}, findings using it={share:.0%}"
        elif kind == "suppress_story":
            hits = [f["id"] for f in findings if matches(f, stories[check["story_id"]], check_period=False)]
            passed, detail = not hits, f"still reported in {hits}" if hits else ""
        elif kind == "preference":
            actual = getattr(harness.preferences, check["key"])
            passed, detail = actual == check["value"], f"{check['key']}={actual}"
        elif kind == "exclusion":
            passed = await excludes(db, harness, _nested(check["field"], check["sample_value"]))
        elif kind == "template_status":
            kw, want = check["keyword"], check["status"]
            found = [t for t in harness.knowledge.templates if kw in f"{t.name} {t.description}".lower()]
            if want == "on":
                passed = any(t.status == "on" and t.id in template_runs for t in found)
            else:
                passed = bool(found) and all(t.status == "off" for t in found)
            detail = ", ".join(f"{t.id}:{t.status}" for t in found) or "no matching template"
        elif kind == "ordering":
            impacts = [f.get("dollar_impact") for f in findings]
            if all(v is not None for v in impacts):
                sizes = [abs(v) for v in impacts]
                passed = sizes == sorted(sizes, reverse=True)
            detail = f"dollar impacts {impacts}"
        compliance.append(Check(id=c["id"], type=kind, passed=passed, detail=detail))

    # Objectives: active ones must be reported with the right value.
    objectives: list[Check] = []
    reported = [o["value"] for o in report_doc["report"].get("objectives", [])]
    for c in earlier:
        check = c["check"]
        if not check or check["type"] != "objective" or as_of > date.fromisoformat(check["scored_until"]):
            continue
        truth = await reference_value(db, check["reference"], as_of)
        close = [v for v in reported if truth is not None and abs(v - truth) <= check["tolerance"]]
        objectives.append(
            Check(id=c["id"], type="objective", passed=bool(close), detail=f"reference={truth}, reported={reported}")
        )

    # Reproducibility: re-run every finding's pipeline as the analyst saw the data.
    data = CompanyData(db, harness, as_of=datetime.combine(as_of, datetime.max.time()), time_fields=time_fields)
    reproducible: list[Check] = []
    for f in findings:
        try:
            rows = await data.aggregate(f["pipeline"]["collection"], f["pipeline"]["stages"])
            reproducible.append(Check(id=f["id"], type="pipeline", passed=bool(rows)))
        except Exception as e:  # noqa: BLE001  (a failing pipeline scores zero)
            reproducible.append(Check(id=f["id"], type="pipeline", passed=False, detail=str(e)[:200]))

    parts = {
        "recall": _part(recall),
        "compliance": _part(compliance),
        "objectives": _part(objectives),
        "reproducibility": _part(reproducible),
    }
    scored = [p.value for p in parts.values() if p.value is not None]
    return Scorecard(
        company=company,
        cycle=cycle,
        as_of=as_of.isoformat(),
        harness_version=report_doc["harness_version"],
        total=round(sum(scored) / len(scored), 4) if scored else 0.0,
        other_findings=len(findings) - len(matched_findings),
        **parts,
    )
