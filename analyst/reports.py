"""Report storage in MongoDB (`reports`)."""

from datetime import UTC, date, datetime

from bson import ObjectId
from pymongo import DESCENDING
from pymongo.asynchronous.database import AsyncDatabase

from analyst.agent import Analysis
from analyst.report_types import GENERAL


async def save(
    db: AsyncDatabase,
    workspace_id: str,
    harness_version: int,
    analysis: Analysis,
    as_of: date,
    report_type: dict | None = None,
) -> ObjectId:
    result = await db.reports.insert_one(
        {
            "workspace_id": workspace_id,
            "harness_version": harness_version,
            "as_of": as_of.isoformat(),
            "report_type": {k: (report_type or GENERAL)[k] for k in ("type_id", "name")},
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


def type_filter(type_id: str) -> dict:
    """Reports of one type. Reports saved before types existed count as general reviews."""
    if type_id == "general":
        return {"$or": [{"report_type.type_id": "general"}, {"report_type": {"$exists": False}}]}
    return {"report_type.type_id": type_id}


async def latest(db: AsyncDatabase, workspace_id: str, type_id: str | None = None) -> dict | None:
    query = {"workspace_id": workspace_id, **(type_filter(type_id) if type_id else {})}
    return await db.reports.find_one(query, sort=[("created_at", DESCENDING)])
