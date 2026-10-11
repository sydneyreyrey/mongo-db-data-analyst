"""Report storage in MongoDB (`reports`)."""

from datetime import UTC, date, datetime

from bson import ObjectId
from pymongo import DESCENDING
from pymongo.asynchronous.database import AsyncDatabase

from analyst.agent import Analysis


async def save(db: AsyncDatabase, workspace_id: str, harness_version: int, analysis: Analysis, as_of: date) -> ObjectId:
    result = await db.reports.insert_one(
        {
            "workspace_id": workspace_id,
            "harness_version": harness_version,
            "as_of": as_of.isoformat(),
            "created_at": datetime.now(UTC),
            "report": analysis.report.model_dump(),
            "charts": analysis.charts,
            "template_runs": [t.model_dump() for t in analysis.template_runs],
            "template_charts": analysis.template_charts,
            "steps": [s.model_dump() for s in analysis.steps],
            "turns": analysis.turns,
            "usage": [u.model_dump() for u in analysis.usage],
            "cost_usd": analysis.cost_usd,
        }
    )
    return result.inserted_id


async def latest(db: AsyncDatabase, workspace_id: str) -> dict | None:
    return await db.reports.find_one({"workspace_id": workspace_id}, sort=[("created_at", DESCENDING)])
