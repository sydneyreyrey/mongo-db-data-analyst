"""Dream: consolidate working memory into long-term memory, like sleep.

The analyst reviews the lessons it picked up in conversations, any pending comments, objectives that are off
track, and its recent reports. It keeps what should guide future work, merges duplicates, settles
contradictions, drops one-offs and lessons that do not hold up against the data, retires notebook entries
the lessons supersede, and writes a short journal. The result is one new harness version (`created_by:
dream`). In production this runs on a schedule (nightly); in the demo it is a button.
"""

import json
from typing import Literal

from pydantic import BaseModel, Field

from analyst.data import CompanyData
from analyst.prompt_builder import render
from analyst.reflect import EDIT_GUIDE, Proposal, _check, _report_brief, propose_loop
from analyst.tools import TOOLS
from common import llm
from harness.edits import Edit
from harness.schema import Harness

DREAM_INSTRUCTIONS = (
    """You are dreaming: consolidating working memory into long-term memory (the playbook above).

Review everything new: lessons from recent conversations, pending comments, objectives that are off track,
and your recent reports. The lessons here have already been confirmed by the reader (they kept them, gave
them as instructions, or came back to them; `strength` and `evidence` show how). Make them permanent unless
they fail against the data or conflict with a newer lesson, and say why if you drop one. Then make the
playbook reflect what you have really learned:
- keep lessons that should guide every future report, written as clean, precise entries
- merge lessons that say the same thing into one entry
- when lessons contradict each other, the more recent one wins unless the data says otherwise
- drop lessons that were one-offs or do not hold up when you check the data
- retire or replace playbook entries that newer lessons supersede
- if an objective is off track, adjust how you analyze so future reports serve it
Check field names and values with the read only tools before writing filters or pipelines.

Every edit's `source` is the id of the lesson, comment, or objective behind it. Give a decision for every
lesson and comment id: applied, merged (into another entry), or dropped, with a short reason. Then write a
journal of 3 to 6 short sentences in the playbook's writing style: what you learned, what you changed, and what
you chose not to keep. Call
`dream` once with everything.

"""
    + EDIT_GUIDE
)


class Decision(BaseModel):
    source: str
    outcome: Literal["applied", "merged", "dropped"]
    reason: str


class DreamInput(BaseModel):
    edits: list[Edit]
    decisions: list[Decision]
    journal: str = Field(min_length=1)


DREAM_TOOL = {
    "name": "dream",
    "description": "Submit the consolidated playbook edits, a decision for every lesson and comment, and the journal.",
    "input_schema": DreamInput.model_json_schema(),
}
DREAM_TOOLS = [t for t in TOOLS if t["name"] != "submit_report"] + [DREAM_TOOL]


class DreamResult(BaseModel):
    edits: list[Edit]
    decisions: list[Decision]
    journal: str
    harness: Harness
    usage: list[llm.Usage]

    @property
    def cost_usd(self) -> float:
        return round(sum(u.cost_usd for u in self.usage), 4)


def off_track(report_doc: dict | None) -> list[str]:
    if not report_doc:
        return []
    return [
        o["objective_id"] for o in report_doc["report"].get("objectives", []) if o["status"] in ("off_track", "missed")
    ]


async def dream(
    harness: Harness,
    data: CompanyData,
    lessons: list[dict],
    comments: list[dict],
    recent_reports: list[dict],
    meter: llm.CostMeter,
) -> DreamResult:
    """`harness` is long-term memory only. `recent_reports` is newest first."""
    goals = off_track(recent_reports[0] if recent_reports else None)
    must_decide = {lesson["_id"] for lesson in lessons} | {c["_id"] for c in comments}
    sources = must_decide | set(goals)
    if not sources:
        return DreamResult(edits=[], decisions=[], journal="Nothing new to consolidate.", harness=harness, usage=[])

    brief = {
        "working_memory_lessons": [
            {
                "id": lesson["_id"],
                "lesson": lesson["text"],
                "edits": lesson["edits"],
                "learned_at": lesson["created_at"],
            }
            for lesson in lessons
        ],
        "pending_comments": [{"id": c["_id"], "text": c["text"], "kind": c.get("kind")} for c in comments],
        "off_track_objectives": goals,
        "recent_reports": [_report_brief(r, []) for r in recent_reports],
    }
    system = [*render(harness), {"type": "text", "text": DREAM_INSTRUCTIONS}]

    async def accept(payload: dict) -> tuple[DreamInput, Harness]:
        result = DreamInput.model_validate(payload)
        new = await _check(data, harness, Proposal(edits=result.edits), sources, set())
        decided = {d.source for d in result.decisions}
        missing = sorted(must_decide - decided)
        if missing:
            raise ValueError(f"Give a decision (applied, merged, or dropped) for {missing}, then call dream again.")
        return result, new

    (result, new), usage, _ = await propose_loop(
        "dream", system, json.dumps(brief, default=str, indent=1), data, DREAM_TOOLS, "dream", accept, meter
    )
    return DreamResult(edits=result.edits, decisions=result.decisions, journal=result.journal, harness=new, usage=usage)
