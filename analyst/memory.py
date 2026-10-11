"""Working memory: lessons picked up in chat, used right away but not yet permanent.

Like a person's day: lessons are used immediately in every conversation, then consolidated into long-term
memory (the harness) when the analyst dreams. Stored in MongoDB `working_memory`:

    { _id, workspace_id, conversation_id, message_id, text, edits, status, created_at,
      dream_id?, outcome?, reason?, consolidated_in_version? }

status: working (waiting for the next dream), consolidated (made permanent), dropped (the dream decided it
should not be kept), forgotten (the reader removed it before a dream).
"""

from datetime import UTC, datetime

from bson import ObjectId
from pymongo import ASCENDING
from pymongo.asynchronous.database import AsyncDatabase

from harness.edits import Edit, InvalidEdit, apply_edit
from harness.locked import LockedFieldError
from harness.schema import Harness


async def ensure_indexes(db: AsyncDatabase) -> None:
    await db.working_memory.create_index(
        [("workspace_id", ASCENDING), ("status", ASCENDING), ("created_at", ASCENDING)]
    )


async def remember(
    db: AsyncDatabase, workspace_id: str, conversation_id: str, message_id: str, text: str, edits: list[Edit]
) -> dict:
    await ensure_indexes(db)
    lesson = {
        "_id": f"lesson_{ObjectId()}",
        "workspace_id": workspace_id,
        "conversation_id": conversation_id,
        "message_id": message_id,
        "text": text,
        "edits": [e.model_dump() for e in edits],
        "status": "working",
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


def working_view(long_term: Harness, lessons: list[dict]) -> tuple[Harness, list[str]]:
    """Long-term memory with today's lessons laid on top. Lessons that no longer apply are skipped."""
    h, skipped = long_term, []
    for lesson in lessons:
        try:
            for e in lesson["edits"]:
                h = apply_edit(h, Edit.model_validate(e))
        except (InvalidEdit, LockedFieldError):
            skipped.append(lesson["_id"])
    view = h.model_copy(update={"version": long_term.version, "parent_version": long_term.parent_version})
    return view, skipped


def working_memory_note(lessons: list[dict]) -> str:
    if not lessons:
        return ""
    lines = "\n".join(f"- {lesson['text']}" for lesson in lessons)
    return (
        "## Working memory (learned in recent conversations, already applied above, made permanent at the next "
        f"dream)\n{lines}"
    )
