"""Report storage in MongoDB (`reports`)."""

from datetime import UTC, datetime

from bson import ObjectId
from pymongo import DESCENDING
from pymongo.asynchronous.database import AsyncDatabase

from analyst.agent import Analysis


async def save(db: AsyncDatabase, workspace_id: str, harness_version: int, analysis: Analysis) -> ObjectId:
    result = await db.reports.insert_one(
        {
            "workspace_id": workspace_id,
            "harness_version": harness_version,
            "created_at": datetime.now(UTC),
            "report": analysis.report.model_dump(),
            "charts": analysis.charts,
            "steps": [s.model_dump() for s in analysis.steps],
            "turns": analysis.turns,
            "usage": [u.model_dump() for u in analysis.usage],
            "cost_usd": analysis.cost_usd,
        }
    )
    return result.inserted_id


async def latest(db: AsyncDatabase, workspace_id: str) -> dict | None:
    return await db.reports.find_one({"workspace_id": workspace_id}, sort=[("created_at", DESCENDING)])
