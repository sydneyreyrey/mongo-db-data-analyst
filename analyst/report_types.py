"""Report types: what a report is for (month-end close, marketing review, ...).

A type has a name and a brief written by a person. The analyst plans each report around its type's brief, and
follows up on the previous report of the same type. What the analyst has learned (exclusions, definitions,
goals) is shared by every type of the company. Stored in MongoDB `report_types`; "general" is built in.
"""

import re
from datetime import UTC, datetime

from pymongo import ASCENDING
from pymongo.asynchronous.database import AsyncDatabase

GENERAL = {
    "type_id": "general",
    "name": "General review",
    "brief": "Whatever matters most for the business right now: trends, drops, spikes, anomalies, and their drivers.",
    "built_in": True,
}


def type_slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:40] or "report"


async def list_types(db: AsyncDatabase, workspace_id: str) -> list[dict]:
    custom = await db.report_types.find(
        {"workspace_id": workspace_id}, {"_id": 0}, sort=[("created_at", ASCENDING)]
    ).to_list()
    return [GENERAL, *custom]


async def get_type(db: AsyncDatabase, workspace_id: str, type_id: str) -> dict:
    if type_id == "general":
        return GENERAL
    doc = await db.report_types.find_one({"workspace_id": workspace_id, "type_id": type_id}, {"_id": 0})
    if not doc:
        raise KeyError(f"No report type {type_id!r}")
    return doc


async def create_type(db: AsyncDatabase, workspace_id: str, name: str, brief: str) -> dict:
    if not name.strip() or not brief.strip():
        raise ValueError("A report type needs a name and a brief")
    await db.report_types.create_index([("workspace_id", ASCENDING), ("type_id", ASCENDING)], unique=True)
    base = type_id = type_slug(name)
    n = 2
    while type_id == "general" or await db.report_types.find_one({"workspace_id": workspace_id, "type_id": type_id}):
        type_id, n = f"{base}_{n}", n + 1
    doc = {
        "_id": f"{workspace_id}:{type_id}",
        "workspace_id": workspace_id,
        "type_id": type_id,
        "name": name.strip(),
        "brief": brief.strip(),
        "built_in": False,
        "created_at": datetime.now(UTC),
    }
    await db.report_types.insert_one(doc)
    return {k: v for k, v in doc.items() if k != "_id"}


async def update_type(db: AsyncDatabase, workspace_id: str, type_id: str, name: str | None, brief: str | None) -> dict:
    if type_id == "general":
        raise ValueError("The built-in general review cannot be changed")
    changes = {k: v.strip() for k, v in (("name", name), ("brief", brief)) if v is not None and v.strip()}
    if changes:
        changes["updated_at"] = datetime.now(UTC)
        await db.report_types.update_one({"workspace_id": workspace_id, "type_id": type_id}, {"$set": changes})
    return await get_type(db, workspace_id, type_id)
