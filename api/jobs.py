"""Background jobs for slow operations (reports, reflection), tracked in MongoDB `jobs`."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from bson import ObjectId
from pymongo.asynchronous.database import AsyncDatabase

_running: set[asyncio.Task] = set()


class Busy(RuntimeError):
    pass


async def start(db: AsyncDatabase, kind: str, workspace_id: str, work: Callable[[], Awaitable[Any]]) -> dict:
    """Start `work` in the background. One running job per workspace at a time."""
    if await db.jobs.find_one({"workspace_id": workspace_id, "status": "running"}):
        raise Busy(f"A job is already running for {workspace_id!r}")
    job = {
        "_id": str(ObjectId()),
        "kind": kind,
        "workspace_id": workspace_id,
        "status": "running",
        "created_at": datetime.now(UTC),
    }
    await db.jobs.insert_one(job)

    async def run() -> None:
        try:
            result = await work()
            update = {"status": "done", "result": result}
        except Exception as e:  # noqa: BLE001  (recorded on the job for the caller to see)
            update = {"status": "failed", "error": f"{type(e).__name__}: {e}"[:2000]}
        await db.jobs.update_one({"_id": job["_id"]}, {"$set": {**update, "finished_at": datetime.now(UTC)}})

    task = asyncio.create_task(run())
    _running.add(task)
    task.add_done_callback(_running.discard)
    return job


async def mark_interrupted(db: AsyncDatabase) -> None:
    """On startup, jobs left running by a previous process can never finish."""
    await db.jobs.update_many({"status": "running"}, {"$set": {"status": "failed", "error": "server restarted"}})
