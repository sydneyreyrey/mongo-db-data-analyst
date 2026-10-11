"""Chat mode: talk with the analyst about a report; it answers and learns in real time.

Each reply can do two things: answer the reader (checking the data with read only tools), and, when the
reader says how they want analysis done from now on, record that in the harness immediately with the
`learn` tool. Learned edits are validated exactly like Reflect's and saved as a new harness version that
every later reply and report uses.
"""

import json
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from analyst.data import CompanyData
from analyst.prompt_builder import render
from analyst.reflect import EDIT_GUIDE, Proposal, _check, _report_brief
from analyst.tools import TOOLS, run_tool
from common import llm
from common.settings import get_settings
from harness.edits import Action, Edit
from harness.schema import Harness

MAX_TURNS = 8

CHAT_INSTRUCTIONS = (
    """You are now chatting with the reader about your latest report. Reply briefly and directly.

When they ask a question, answer it, checking the data with the read only tools instead of guessing numbers.

When a message tells you how they want analysis done from now on (what to include or exclude, which metrics
matter, what a term means, goals, time grain, views to keep or stop), record it right away with the `learn`
tool, then confirm in one short sentence what you will do differently. Edits are permanent until changed.
Do not learn from questions, small talk, reactions, or one-off requests ("just this time"). If you cannot
tell whether they mean it permanently, ask before learning. Check field names and real values with the
tools before writing filters or pipelines.

"""
    + EDIT_GUIDE
)


class LearnEdit(BaseModel):
    action: Action
    target: str = Field(min_length=1)
    payload: dict[str, Any] = {}
    rationale: str = Field(min_length=1)


class LearnInput(BaseModel):
    edits: list[LearnEdit]


LEARN_TOOL = {
    "name": "learn",
    "description": "Record how the reader wants analysis done from now on, as playbook edits. Saved immediately.",
    "input_schema": LearnInput.model_json_schema(),
}
CHAT_TOOLS = [t for t in TOOLS if t["name"] != "submit_report"] + [LEARN_TOOL]


class Learned(BaseModel):
    edits: list[Edit]
    version: int


class Reply(BaseModel):
    text: str
    learned: list[Learned]
    usage: list[llm.Usage]

    @property
    def cost_usd(self) -> float:
        return round(sum(u.cost_usd for u in self.usage), 4)


class ChatFailed(RuntimeError):
    pass


def context_message(report_doc: dict | None, pointing_at: dict | None) -> str:
    parts = []
    if report_doc:
        brief = _report_brief(report_doc, [])
        parts.append("Your latest report:\n" + json.dumps(brief, default=str, indent=1))
    if pointing_at:
        parts.append("The reader is pointing at this item:\n" + json.dumps(pointing_at, default=str, indent=1))
    return "\n\n".join(parts)


async def chat(
    harness: Harness,
    make_data: Callable[[Harness], CompanyData],
    history: list[dict[str, str]],
    message: str,
    message_id: str,
    report_doc: dict | None,
    pointing_at: dict | None,
    save: Callable[[Harness, list[Edit]], Awaitable[Harness]],
    meter: llm.CostMeter,
) -> Reply:
    """One reader message in, one reply out. `save` persists learned edits and returns the new harness."""
    settings = get_settings()
    system = [*render(harness), {"type": "text", "text": CHAT_INSTRUCTIONS}]
    data = make_data(harness)
    context = context_message(report_doc, pointing_at)
    messages: list[dict[str, Any]] = [{"role": m["role"], "content": m["text"]} for m in history if m.get("text")]
    messages.append({"role": "user", "content": f"{context}\n\nReader: {message}" if context else message})
    learned: list[Learned] = []
    usage: list[llm.Usage] = []

    for _ in range(MAX_TURNS):
        response, u = await llm.call(
            "chat",
            meter,
            model=settings.analyst_model,
            max_tokens=8000,
            output_config={"effort": "low"},
            system=system,
            tools=CHAT_TOOLS,
            messages=messages,
        )
        usage.append(u)
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason == "refusal":
            raise ChatFailed(f"Model refused: {getattr(response, 'stop_details', None)}")
        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            text = "".join(b.text for b in response.content if b.type == "text").strip()
            return Reply(text=text, learned=learned, usage=usage)

        results = []
        for block in tool_uses:
            ok = True
            if block.name == "learn":
                try:
                    edits = [
                        Edit(**e.model_dump(), source=message_id) for e in LearnInput.model_validate(block.input).edits
                    ]
                    await _check(data, harness, Proposal(edits=edits), {message_id}, set())
                except (ValidationError, ValueError) as e:
                    ok, text = False, str(e)
                else:
                    harness = await save(harness, edits)
                    data = make_data(harness)  # later queries in this reply see the new exclusions
                    learned.append(Learned(edits=edits, version=harness.version))
                    text = f"Saved as playbook v{harness.version}."
            else:
                try:
                    text = await run_tool(data, block.name, block.input)
                except Exception as e:  # noqa: BLE001  (query errors go back to the model)
                    ok, text = False, f"{type(e).__name__}: {e}"
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": text, "is_error": not ok})
        messages.append({"role": "user", "content": results})

    raise ChatFailed(f"No reply after {MAX_TURNS} turns")
