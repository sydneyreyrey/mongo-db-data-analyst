"""Versioned harness storage in MongoDB (`workspaces`, `harness_versions`)."""

from datetime import UTC, datetime

from pymongo import ASCENDING, DESCENDING
from pymongo.asynchronous.database import AsyncDatabase

from harness.diff import diff
from harness.edits import Edit, apply_edit
from harness.schema import Harness
from harness.seed import seed_harness


async def ensure_indexes(db: AsyncDatabase) -> None:
    await db.harness_versions.create_index([("workspace_id", ASCENDING), ("version", ASCENDING)], unique=True)


async def create_workspace(
    db: AsyncDatabase,
    workspace_id: str,
    company_db: str,
    time_fields: dict[str, str] | None = None,
    name: str = "",
    source: str = "",
) -> Harness:
    """Create a workspace with seed harness v1. Returns the existing latest harness if present."""
    await ensure_indexes(db)
    existing = await db.workspaces.find_one({"_id": workspace_id})
    if existing:
        return await latest(db, workspace_id)
    await db.workspaces.insert_one(
        {
            "_id": workspace_id,
            "name": name or workspace_id,
            "company_db": company_db,
            "time_fields": time_fields or {},
            "source": source,
            "created_at": datetime.now(UTC),
        }
    )
    harness = seed_harness(workspace_id)
    await _insert(db, harness, edits=[], created_by="seed")
    return harness


async def get_workspace(db: AsyncDatabase, workspace_id: str) -> dict:
    doc = await db.workspaces.find_one({"_id": workspace_id})
    if not doc:
        raise KeyError(f"No workspace {workspace_id!r}")
    return doc


async def list_workspaces(db: AsyncDatabase) -> list[dict]:
    return await db.workspaces.find({}, sort=[("created_at", ASCENDING)]).to_list()


async def set_time_fields(db: AsyncDatabase, workspace_id: str, time_fields: dict[str, str]) -> None:
    """Connection metadata, not learned knowledge: a person confirms or corrects the detected date fields."""
    result = await db.workspaces.update_one({"_id": workspace_id}, {"$set": {"time_fields": time_fields}})
    if not result.matched_count:
        raise KeyError(f"No workspace {workspace_id!r}")


async def _insert(db: AsyncDatabase, harness: Harness, edits: list[Edit], created_by: str) -> None:
    await db.harness_versions.insert_one(
        {
            "workspace_id": harness.workspace_id,
            "version": harness.version,
            "parent_version": harness.parent_version,
            "harness": harness.model_dump(),
            "edits": [e.model_dump() for e in edits],
            "created_by": created_by,
            "created_at": datetime.now(UTC),
        }
    )


async def get(db: AsyncDatabase, workspace_id: str, version: int) -> Harness:
    doc = await db.harness_versions.find_one({"workspace_id": workspace_id, "version": version})
    if not doc:
        raise KeyError(f"No harness v{version} for {workspace_id!r}")
    return Harness.model_validate(doc["harness"])


async def latest(db: AsyncDatabase, workspace_id: str) -> Harness:
    doc = await db.harness_versions.find_one({"workspace_id": workspace_id}, sort=[("version", DESCENDING)])
    if not doc:
        raise KeyError(f"No harness for {workspace_id!r}")
    return Harness.model_validate(doc["harness"])


async def apply_edits(db: AsyncDatabase, base: Harness, edits: list[Edit], created_by: str = "reflect") -> Harness:
    """Apply a round of edits to `base` and save the result as one new version.

    Every edit is validated; if any one is invalid or touches a locked field, nothing is saved.
    """
    harness = base
    for edit in edits:
        harness = apply_edit(harness, edit)
    harness.parent_version = base.version
    harness.version = (await latest(db, base.workspace_id)).version + 1
    await _insert(db, harness, edits, created_by)
    return harness


async def revert_to(db: AsyncDatabase, workspace_id: str, version: int) -> Harness:
    """Roll back by saving a copy of an earlier version as the newest version (history is kept)."""
    target = await get(db, workspace_id, version)
    current = await latest(db, workspace_id)
    restored = target.model_copy(update={"version": current.version + 1, "parent_version": version})
    await _insert(db, restored, edits=[], created_by=f"revert:v{current.version}")
    return restored


async def history(db: AsyncDatabase, workspace_id: str) -> list[dict]:
    cursor = db.harness_versions.find(
        {"workspace_id": workspace_id}, {"_id": 0, "harness": 0}, sort=[("version", ASCENDING)]
    )
    return await cursor.to_list()


async def version_diff(db: AsyncDatabase, workspace_id: str, old: int, new: int) -> list[str]:
    return diff(await get(db, workspace_id, old), await get(db, workspace_id, new))
