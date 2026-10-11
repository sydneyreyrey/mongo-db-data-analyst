"""Working memory: lessons picked up in conversation, used right away but not yet permanent.

Like a person's day: lessons apply immediately in every conversation, and are consolidated into long-term
memory (the harness) when the analyst dreams. What lands is decided by evidence, not by the model alone:
each lesson has a strength that grows when the reader confirms or repeats it and drops with pushback. Only
strong lessons reach a dream; weak ones wait, and fade if nothing reinforces them.

Stored in MongoDB `working_memory`:

    { _id, workspace_id, conversation_id, message_id, text, edits, status, strength, evidence, dreams_seen,
      created_at, dream_id?, outcome?, reason?, consolidated_in_version? }

status: working (waiting), consolidated (made permanent), dropped (a dream decided not to keep it),
faded (never reinforced), forgotten (removed by the reader).
"""

from datetime import UTC, datetime

from bson import ObjectId
from pymongo import ASCENDING
from pymongo.asynchronous.database import AsyncDatabase

from harness.edits import Edit, InvalidEdit, apply_edit
from harness.locked import LockedFieldError
from harness.schema import Harness

# How much each kind of evidence counts. A lesson needs PROMOTE_AT to be considered at a dream.
STRENGTH = {
    "instruction": 2,  # the reader told the analyst how to work from now on, in chat
    "kept": 3,  # the reader pressed "remember this" on an exploration step
    "repeated": 1,  # the same idea came up again
    "pushback": -2,  # the reader rejected it
}
PROMOTE_AT = 2
FADE_AFTER_DREAMS = 2  # weak lessons that sit through this many dreams without reinforcement fade


async def ensure_indexes(db: AsyncDatabase) -> None:
    await db.working_memory.create_index(
        [("workspace_id", ASCENDING), ("status", ASCENDING), ("created_at", ASCENDING)]
    )


def _signature(edits: list[dict]) -> set[tuple[str, str]]:
    """What a lesson is about: the (action, target) pairs it changes."""
    return {(e["action"], e["target"]) for e in edits}


async def find_matching(db: AsyncDatabase, workspace_id: str, edits: list[dict]) -> dict | None:
    """A working lesson that changes the same things, if any."""
    wanted = _signature(edits)
    for lesson in await working_lessons(db, workspace_id):
        if wanted & _signature(lesson["edits"]):
            return lesson
    return None


async def reinforce(db: AsyncDatabase, lesson_id: str, kind: str, ref: str = "") -> dict:
    """Add evidence for (or against) a lesson."""
    return await db.working_memory.find_one_and_update(
        {"_id": lesson_id},
        {
            "$inc": {"strength": STRENGTH[kind]},
            "$push": {"evidence": {"kind": kind, "ref": ref, "at": datetime.now(UTC)}},
        },
        return_document=True,
    )


async def remember(
    db: AsyncDatabase,
    workspace_id: str,
    conversation_id: str,
    message_id: str,
    text: str,
    edits: list[Edit],
    kind: str = "instruction",
) -> dict:
    """Store a lesson, or strengthen the existing one if the reader already taught the same thing."""
    await ensure_indexes(db)
    dumped = [e.model_dump() for e in edits]
    existing = await find_matching(db, workspace_id, dumped)
    if existing:
        await db.working_memory.update_one({"_id": existing["_id"]}, {"$set": {"text": text, "edits": dumped}})
        return await reinforce(db, existing["_id"], "repeated" if kind == "instruction" else kind, message_id)
    lesson = {
        "_id": f"lesson_{ObjectId()}",
        "workspace_id": workspace_id,
        "conversation_id": conversation_id,
        "message_id": message_id,
        "text": text,
        "edits": dumped,
        "status": "working",
        "strength": STRENGTH[kind],
        "evidence": [{"kind": kind, "ref": message_id, "at": datetime.now(UTC)}],
        "dreams_seen": 0,
        "created_at": datetime.now(UTC),
    }
    await db.working_memory.insert_one(lesson)
    return lesson


async def working_lessons(db: AsyncDatabase, workspace_id: str) -> list[dict]:
    return await db.working_memory.find(
        {"workspace_id": workspace_id, "status": "working"}, sort=[("created_at", ASCENDING)]
    ).to_list()


async def all_lessons(db: AsyncDatabase, workspace_id: str) -> list[dict]:
    return await db.working_memory.find({"workspace_id": workspace_id}, sort=[("created_at", ASCENDING)]).to_list()


async def forget(db: AsyncDatabase, workspace_id: str, lesson_id: str) -> dict:
    """Remove a lesson from working memory before it is consolidated."""
    result = await db.working_memory.find_one_and_update(
        {"_id": lesson_id, "workspace_id": workspace_id, "status": "working"},
        {"$set": {"status": "forgotten", "forgotten_at": datetime.now(UTC)}},
        return_document=True,
    )
    if not result:
        raise KeyError(f"No working lesson {lesson_id!r}")
    return result


def split_for_dream(lessons: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """(ready for the dream, still waiting, fading now). Plain rules, no model judgment."""
    ready, waiting, fading = [], [], []
    for lesson in lessons:
        if lesson.get("strength", PROMOTE_AT) >= PROMOTE_AT:
            ready.append(lesson)
        elif lesson.get("dreams_seen", 0) + 1 >= FADE_AFTER_DREAMS:
            fading.append(lesson)
        else:
            waiting.append(lesson)
    return ready, waiting, fading


def working_view(long_term: Harness, lessons: list[dict]) -> tuple[Harness, list[str]]:
    """Long-term memory with working lessons laid on top. Lessons that no longer apply are skipped."""
    h, skipped = long_term, []
    for lesson in lessons:
        if lesson.get("strength", PROMOTE_AT) <= 0:
            continue
        try:
            for e in lesson["edits"]:
                h = apply_edit(h, Edit.model_validate(e))
        except (InvalidEdit, LockedFieldError):
            skipped.append(lesson["_id"])
    view = h.model_copy(update={"version": long_term.version, "parent_version": long_term.parent_version})
    return view, skipped


def working_memory_note(lessons: list[dict]) -> str:
    active = [lesson for lesson in lessons if lesson.get("strength", PROMOTE_AT) > 0]
    if not active:
        return ""
    lines = "\n".join(f"- {lesson['text']}" for lesson in active)
    return (
        "## Working memory (learned in recent conversations, already applied above, made permanent at the next "
        f"dream)\n{lines}"
    )
