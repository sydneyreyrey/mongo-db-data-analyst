"""Reflect mode: turn a reader's comments (and off track objectives) into typed harness edits.

The same model as Analyze mode, with the same read only tools, plus `propose_edits`. Proposed edits are
validated against the harness and the data before they are accepted; problems go back to the model.
"""

import json
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from analyst.data import CompanyData
from analyst.prompt_builder import render
from analyst.report import vega_lite
from analyst.tools import TOOLS, run_tool
from common import llm
from common.settings import get_settings
from harness.edits import Edit, InvalidEdit, apply_edit
from harness.locked import LockedFieldError
from harness.schema import Harness

MAX_TURNS = 10

EDIT_GUIDE = """Edit actions (target, then payload):
- annotate_collection: collection name; {"note"}
- set_vocab / remove_vocab: term; {"definition"}
- add_rule / retire_rule: rule id; {"text"}
- set_focus_metric / remove_focus_metric: metric name; {"definition": exact computation}
- add_exclusion / remove_exclusion: exclusion id; {"filter": MongoDB filter on raw documents, "reason"}
- set_preference: time_grain (day, week, month, quarter) | max_findings (1-10) | chart_style; {"value"}
- set_objective / retire_objective: objective id; {"text", "metric": exact computation, "target": number,
  "direction": "at_least" | "at_most", "deadline": "YYYY-MM-DD"}
- save_template: template id; {"name", "description", "pipeline": {"collection", "stages"},
  "chart": {"type": line | bar | area | point, "x", "y", "color" (optional), "title"}}
- set_template_status: template id; {"status": "on" | "off"}"""

REFLECT_INSTRUCTIONS = (
    """You maintain the playbook above. You are now in Reflect mode, not writing a report.

Turn each new comment into the smallest set of playbook edits that makes every future report follow it.
Edits are permanent until changed, so write them for future reviews, not just the last one. Prefer precise,
checkable entries (an exclusion filter, a focus metric with an exact definition, a preference value) over
vague rules. Use the read only tools to check field names and real values before writing filters or pipelines.
Every edit's `source` is the id of the comment it answers. If an objective is off track, you may also add
edits aimed at it, with the objective id as `source`. If a comment cannot be expressed as an edit, explain
why in `notes` instead. Then call propose_edits once with everything.

"""
    + EDIT_GUIDE
)


class Comment(BaseModel):
    id: str
    text: str
    kind: Literal["comment", "keep", "stop"] = "comment"
    finding_id: str | None = None
    template_id: str | None = None


class Note(BaseModel):
    source: str
    note: str


class Proposal(BaseModel):
    edits: list[Edit]
    notes: list[Note] = []


PROPOSE_TOOL = {
    "name": "propose_edits",
    "description": "Submit the playbook edits for these comments, plus notes for anything not turned into an edit.",
    "input_schema": Proposal.model_json_schema(),
}
REFLECT_TOOLS = [t for t in TOOLS if t["name"] != "submit_report"] + [PROPOSE_TOOL]


class Reflection(BaseModel):
    edits: list[Edit]
    notes: list[Note]
    harness: Harness
    usage: list[llm.Usage]
    turns: int

    @property
    def cost_usd(self) -> float:
        return round(sum(u.cost_usd for u in self.usage), 4)


class ReflectFailed(RuntimeError):
    pass


def _report_brief(report_doc: dict, comments: list[Comment]) -> dict:
    """What Reflect sees of the last report: titles and metrics, plus full detail for findings a comment targets."""
    targeted = {c.finding_id for c in comments if c.finding_id}
    r = report_doc["report"]
    findings = []
    for f in r["findings"]:
        item = {k: f[k] for k in ("id", "title", "metric", "segment", "period", "direction", "magnitude")}
        if f["id"] in targeted:
            item["pipeline"], item["chart"] = f["pipeline"], f["chart"]
        findings.append(item)
    return {
        "as_of": report_doc.get("as_of"),
        "summary": r["summary"],
        "findings": findings,
        "objectives": [
            {k: o[k] for k in ("objective_id", "value", "target", "status", "note")} for o in r.get("objectives", [])
        ],
        "saved_views_shown": [t["id"] for t in report_doc.get("template_runs", [])],
    }


async def _check(
    data: CompanyData, harness: Harness, proposal: Proposal, sources: set[str], comment_ids: set[str]
) -> Harness:
    """Apply every edit to a copy and test it against the data. Raises ValueError listing all problems."""
    problems = []
    h = harness
    for i, edit in enumerate(proposal.edits):
        label = f"edit {i} ({edit.action} {edit.target})"
        if edit.source not in sources:
            problems.append(f"{label}: unknown source {edit.source!r}; use one of {sorted(sources)}")
            continue
        try:
            h = apply_edit(h, edit)
        except (InvalidEdit, LockedFieldError) as e:
            problems.append(f"{label}: {e}")
            continue
        if edit.action == "add_exclusion":
            try:
                for coll in sorted(await data.collections()):
                    if await data.aggregate(coll, [{"$match": edit.payload["filter"]}, {"$count": "n"}]):
                        break
                else:
                    problems.append(f"{label}: filter matches no documents in any collection; check fields and values")
            except Exception as e:  # noqa: BLE001  (reported back to the model)
                problems.append(f"{label}: filter does not run: {e}")
        if edit.action == "save_template":
            t = next(t for t in h.knowledge.templates if t.id == edit.target)
            try:
                rows = await data.aggregate(t.pipeline.collection, t.pipeline.stages)
                if not rows:
                    raise ValueError("pipeline returned no rows")
                vega_lite(t.chart, rows)
                missing = [c for c in (t.chart.x, t.chart.y, t.chart.color) if c and c not in rows[0]]
                if missing:
                    raise ValueError(f"chart fields {missing} not in output keys {sorted(rows[0])}")
            except Exception as e:  # noqa: BLE001  (reported back to the model)
                problems.append(f"{label}: template does not run: {e}")

    addressed = {e.source for e in proposal.edits} | {n.source for n in proposal.notes}
    unanswered = sorted(comment_ids - addressed)
    if unanswered:
        problems.append(f"No edit or note for comments {unanswered}")
    if problems:
        raise ValueError("Fix these and call propose_edits again:\n" + "\n".join(problems))
    return h


async def reflect(
    harness: Harness,
    data: CompanyData,
    report_doc: dict,
    comments: list[Comment],
    meter: llm.CostMeter,
) -> Reflection:
    settings = get_settings()
    off_track = [
        o["objective_id"] for o in report_doc["report"].get("objectives", []) if o["status"] in ("off_track", "missed")
    ]
    sources = {c.id for c in comments} | set(off_track)
    if not sources:
        return Reflection(edits=[], notes=[], harness=harness, usage=[], turns=0)

    system = [*render(harness), {"type": "text", "text": REFLECT_INSTRUCTIONS}]
    brief = {
        "last_report": _report_brief(report_doc, comments),
        "new_comments": [c.model_dump(exclude_none=True) for c in comments],
        "off_track_objectives": off_track,
    }
    messages: list[dict[str, Any]] = [{"role": "user", "content": json.dumps(brief, default=str, indent=1)}]
    usage: list[llm.Usage] = []

    for turn in range(1, MAX_TURNS + 1):
        response, u = await llm.call(
            "reflect",
            meter,
            model=settings.analyst_model,
            max_tokens=16000,
            output_config={"effort": "medium"},
            system=system,
            tools=REFLECT_TOOLS,
            messages=messages,
        )
        usage.append(u)
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason == "refusal":
            raise ReflectFailed(f"Model refused: {getattr(response, 'stop_details', None)}")
        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            messages.append({"role": "user", "content": "Call propose_edits with your edits and notes."})
            continue

        results = []
        for block in tool_uses:
            ok = True
            if block.name == "propose_edits":
                try:
                    proposal = Proposal.model_validate(block.input)
                    new = await _check(data, harness, proposal, sources, {c.id for c in comments})
                except ValidationError as e:
                    ok, text = False, f"Proposal does not match the schema: {e}"
                except ValueError as e:
                    ok, text = False, str(e)
                else:
                    return Reflection(edits=proposal.edits, notes=proposal.notes, harness=new, usage=usage, turns=turn)
            else:
                try:
                    text = await run_tool(data, block.name, block.input)
                except Exception as e:  # noqa: BLE001  (query errors go back to the model)
                    ok, text = False, f"{type(e).__name__}: {e}"
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": text, "is_error": not ok})
        messages.append({"role": "user", "content": results})

    raise ReflectFailed(f"No valid proposal after {MAX_TURNS} turns")
