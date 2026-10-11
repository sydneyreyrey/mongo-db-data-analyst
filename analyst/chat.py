"""Conversations with the analyst, in two modes.

chat:    it answers, and when the reader says how they want analysis done from now on, keeps that as a lesson
         in working memory (`learn`). Lessons apply right away in conversations and become permanent (the
         harness, which reports read) when the analyst dreams.
explore: back and forth what-ifs. Each result is shown as a step with its own chart (`show`); nothing is
         learned. A step can carry a candidate lesson, and the reader decides what lands ("remember this").

All edits are validated exactly like Reflect's, against the real data.
"""

import json
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from analyst.agent import SANITY_CHECK
from analyst.data import CompanyData
from analyst.prompt_builder import render
from analyst.reflect import EDIT_GUIDE, Proposal, _check, _report_brief
from analyst.report import chart_problems, vega_lite
from analyst.tools import TOOLS, run_tool
from common import llm
from common.settings import get_settings
from harness.edits import Action, Edit
from harness.schema import Chart, Harness, Pipeline

MAX_TURNS = 8

CHAT_INSTRUCTIONS = (
    """You are now chatting with the reader about your latest report. Reply briefly and directly, in the playbook's
writing style.

When they ask a question, answer it, checking the data with the read only tools instead of guessing numbers.
"""
    + SANITY_CHECK
    + """

When a message tells you how they want analysis done from now on (what to include or exclude, which metrics
matter, what a term means, goals, time grain, views to keep or stop, how reports should be organized or what
story they should tell), record it right away with the `learn`
tool as a lesson, then confirm in one short sentence what you will do differently. Lessons apply at once and
become permanent the next time you dream.
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
    lesson: str = Field(min_length=1, description="The lesson in one plain sentence, e.g. 'Exclude test accounts.'")
    edits: list[LearnEdit]


EXPLORE_INSTRUCTIONS = (
    """You are exploring the data together with the reader. Nothing you do here changes your memory: the reader
decides what lands. Reply briefly, in the playbook's writing style, and let the charts speak.

"""
    + SANITY_CHECK
    + """
Answer their questions and try the what-ifs they ask for ("what if we excluded returns?", "break that down by
week"), or suggest a useful next one. Check everything with the read only tools. Show each meaningful result
with the `show` tool: a title, a one sentence summary of what it shows, the pipeline, and a chart a person with
no technical background can read (plain title stating the takeaway, axis labels in plain words with units, no
field names).

When a step reflects a way of working the reader might want every time (an exclusion, a metric definition, a
time grain, a breakdown worth watching), attach it as `lesson`, with the playbook edits that would express it.
A breakdown worth watching is a save_template edit with the same pipeline and chart. Do not attach lessons to
one-off facts.

"""
    + EDIT_GUIDE
)


class ShowInput(BaseModel):
    title: str = Field(min_length=1)
    summary: str = Field(min_length=1, description="One sentence: what this result shows")
    pipeline: Pipeline
    chart: Chart
    lesson: LearnInput | None = Field(
        default=None, description="Only if this step is a way of working the reader may want every time"
    )


SHOW_TOOL = {
    "name": "show",
    "description": "Show the reader one exploration step: a result with its chart, optionally with a candidate "
    "lesson they can choose to keep. Nothing is learned until they keep it.",
    "input_schema": ShowInput.model_json_schema(),
}

LEARN_TOOL = {
    "name": "learn",
    "description": "Keep a lesson about how the reader wants analysis done from now on, with the playbook edits "
    "that express it. Applies immediately; made permanent at the next dream.",
    "input_schema": LearnInput.model_json_schema(),
}
READ_TOOLS = [t for t in TOOLS if t["name"] != "submit_report"]
CHAT_TOOLS = [*READ_TOOLS, LEARN_TOOL]
EXPLORE_TOOLS = [*READ_TOOLS, SHOW_TOOL]


class Reply(BaseModel):
    text: str
    lessons: list[dict]
    steps: list[dict] = []
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
    working_note: str,
    make_data: Callable[[Harness], CompanyData],
    history: list[dict[str, str]],
    message: str,
    message_id: str,
    report_doc: dict | None,
    pointing_at: dict | None,
    remember: Callable[[str, list[Edit]], Awaitable[dict]],
    meter: llm.CostMeter,
    mode: str = "chat",
    record_step: Callable[[dict], Awaitable[dict]] | None = None,
) -> Reply:
    """One reader message in, one reply out.

    `harness` is the working view (long-term memory plus working lessons). `remember` stores a lesson in working
    memory (chat mode). `record_step` stores an exploration step (explore mode). Both return the stored record.
    """
    settings = get_settings()
    explore = mode == "explore"
    instructions = EXPLORE_INSTRUCTIONS if explore else CHAT_INSTRUCTIONS
    system = [*render(harness), {"type": "text", "text": instructions}]
    if working_note:
        system.insert(-1, {"type": "text", "text": working_note})
    data = make_data(harness)
    context = context_message(report_doc, pointing_at)
    messages: list[dict[str, Any]] = [{"role": m["role"], "content": m["text"]} for m in history if m.get("text")]
    messages.append({"role": "user", "content": f"{context}\n\nReader: {message}" if context else message})
    lessons: list[dict] = []
    steps: list[dict] = []
    usage: list[llm.Usage] = []

    for _ in range(MAX_TURNS):
        response, u = await llm.call(
            "chat",
            meter,
            model=settings.analyst_model,
            max_tokens=8000,
            output_config={"effort": "low"},
            system=system,
            tools=EXPLORE_TOOLS if explore else CHAT_TOOLS,
            messages=messages,
        )
        usage.append(u)
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason == "refusal":
            raise ChatFailed(f"Model refused: {getattr(response, 'stop_details', None)}")
        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            text = "".join(b.text for b in response.content if b.type == "text").strip()
            return Reply(text=text, lessons=lessons, steps=steps, usage=usage)

        results = []
        for block in tool_uses:
            ok = True
            if block.name == "show" and explore and record_step:
                try:
                    step = await _show(data, harness, ShowInput.model_validate(block.input), message_id)
                except (ValidationError, ValueError) as e:
                    ok, text = False, str(e)
                else:
                    stored = await record_step(step)
                    steps.append(stored)
                    text = f"Shown to the reader as step {stored['_id']}."
            elif block.name == "learn" and not explore:
                try:
                    learn = LearnInput.model_validate(block.input)
                    edits = [Edit(**e.model_dump(), source=message_id) for e in learn.edits]
                    updated = await _check(data, harness, Proposal(edits=edits), {message_id}, set())
                except (ValidationError, ValueError) as e:
                    ok, text = False, str(e)
                else:
                    lesson = await remember(learn.lesson, edits)
                    lessons.append(lesson)
                    harness = updated.model_copy(update={"version": harness.version})
                    data = make_data(harness)  # later queries in this reply already follow the lesson
                    text = "Kept in working memory; it applies now and becomes permanent at the next dream."
            else:
                try:
                    text = await run_tool(data, block.name, block.input)
                except Exception as e:  # noqa: BLE001  (query errors go back to the model)
                    ok, text = False, f"{type(e).__name__}: {e}"
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": text, "is_error": not ok})
        messages.append({"role": "user", "content": results})

    raise ChatFailed(f"No reply after {MAX_TURNS} turns")


async def _show(data: CompanyData, harness: Harness, show: ShowInput, message_id: str) -> dict:
    """Run the step's pipeline, build its chart, and check any candidate lesson. Raises ValueError on problems."""
    try:
        rows = await data.aggregate(show.pipeline.collection, show.pipeline.stages)
    except Exception as e:  # noqa: BLE001  (reported back to the model)
        raise ValueError(f"pipeline failed: {e}") from e
    if not rows:
        raise ValueError("pipeline returned no rows")
    missing = [c for c in (show.chart.x, show.chart.y, show.chart.color) if c and c not in rows[0]]
    if missing:
        raise ValueError(f"chart fields {missing} not in pipeline output keys {sorted(rows[0])}")
    unreadable = chart_problems(show.chart, rows)
    if unreadable:
        raise ValueError("; ".join(unreadable))
    candidate = None
    if show.lesson:
        edits = [Edit(**e.model_dump(), source=message_id) for e in show.lesson.edits]
        await _check(data, harness, Proposal(edits=edits), {message_id}, set())
        candidate = {"text": show.lesson.lesson, "edits": [e.model_dump() for e in edits]}
    return {
        "title": show.title,
        "summary": show.summary,
        "pipeline": show.pipeline.model_dump(),
        "chart": show.chart.model_dump(),
        "vega_lite": vega_lite(show.chart, rows),
        "candidate_lesson": candidate,
    }
