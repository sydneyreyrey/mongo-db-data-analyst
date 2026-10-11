"""The operations everything else calls: connect a company, run a report, take comments, reflect.

Used by the API, the CLI scripts, and the simulator, so live use and scored runs behave identically.
"""

import asyncio
from datetime import UTC, date, datetime
from typing import Any

from bson import ObjectId
from pymongo import ASCENDING, DESCENDING
from pymongo.asynchronous.database import AsyncDatabase

from analyst import reports
from analyst.agent import analyze
from analyst.chat import chat
from analyst.compare import compare
from analyst.data import CompanyData
from analyst.onboard import detect_time_fields
from analyst.reflect import Comment, reflect
from common import llm
from common.db import get_client
from common.settings import get_settings
from harness import store
from harness.diff import diff


def _end_of(day: date) -> datetime:
    return datetime.combine(day, datetime.max.time())


async def connect(db: AsyncDatabase, workspace_id: str, company_db: str, name: str = "", source: str = "") -> dict:
    """Register a company database: detect its date fields and create a blank harness v1."""
    if company_db not in await get_client().list_database_names():
        raise KeyError(f"No database {company_db!r} in the cluster")
    time_fields = await detect_time_fields(get_client()[company_db])
    await store.create_workspace(db, workspace_id, company_db, time_fields=time_fields, name=name, source=source)
    ws = await store.get_workspace(db, workspace_id)
    if not ws.get("time_fields"):  # created before detection existed
        await store.set_time_fields(db, workspace_id, time_fields)
        ws["time_fields"] = time_fields
    return ws


async def _company_data(db: AsyncDatabase, workspace_id: str, harness, as_of: date | None) -> tuple[dict, CompanyData]:
    ws = await store.get_workspace(db, workspace_id)
    data = CompanyData(
        get_client()[ws["company_db"]],
        harness,
        as_of=_end_of(as_of) if as_of else None,
        time_fields=ws.get("time_fields", {}),
    )
    return ws, data


async def run_report(db: AsyncDatabase, workspace_id: str, as_of: date | None = None) -> dict:
    """Analyze with the latest harness, as of a review date (default today). Returns the saved report."""
    harness = await store.latest(db, workspace_id)
    ws, data = await _company_data(db, workspace_id, harness, as_of)
    previous = await reports.latest(db, workspace_id)
    previous_titles = [f["title"] for f in previous["report"]["findings"]] if previous else []
    meter = llm.CostMeter(limit_usd=get_settings().max_usd)
    analysis = await analyze(harness, data, ws["company_db"], meter, today=as_of or date.today(),
                             previous=previous_titles)  # fmt: skip
    report_id = await reports.save(db, workspace_id, harness.version, analysis, as_of=as_of or date.today())
    return await db.reports.find_one({"_id": report_id})


async def add_comment(
    db: AsyncDatabase,
    workspace_id: str,
    text: str,
    kind: str = "comment",
    finding_id: str | None = None,
    template_id: str | None = None,
    report_id: str | None = None,
    comment_id: str | None = None,
) -> dict:
    """Store a reader comment. It takes effect at the next reflect."""
    await store.get_workspace(db, workspace_id)
    comment = Comment(
        id=comment_id or f"c_{ObjectId()}",
        text=text,
        kind=kind,
        finding_id=finding_id,
        template_id=template_id,
    )
    doc = {
        **comment.model_dump(),
        "_id": comment.id,
        "workspace_id": workspace_id,
        "report_id": report_id,
        "status": "pending",
        "created_at": datetime.now(UTC),
    }
    await db.comments.insert_one(doc)
    return doc


async def list_comments(db: AsyncDatabase, workspace_id: str, status: str | None = None) -> list[dict]:
    query: dict[str, Any] = {"workspace_id": workspace_id}
    if status:
        query["status"] = status
    return await db.comments.find(query, sort=[("created_at", ASCENDING)]).to_list()


async def run_reflect(db: AsyncDatabase, workspace_id: str) -> dict:
    """Turn pending comments (and off track objectives) into a new harness version."""
    harness = await store.latest(db, workspace_id)
    report_doc = await db.reports.find_one({"workspace_id": workspace_id}, sort=[("created_at", DESCENDING)])
    if not report_doc:
        raise ValueError("Run a report before reflecting")
    pending = await list_comments(db, workspace_id, status="pending")
    comments = [Comment.model_validate(c) for c in pending]
    as_of = date.fromisoformat(report_doc["as_of"]) if report_doc.get("as_of") else None
    _, data = await _company_data(db, workspace_id, harness, as_of)
    meter = llm.CostMeter(limit_usd=get_settings().max_usd)

    reflection = await reflect(harness, data, report_doc, comments, meter)
    result: dict[str, Any] = {
        "from_version": harness.version,
        "edits": [e.model_dump() for e in reflection.edits],
        "notes": [n.model_dump() for n in reflection.notes],
        "cost_usd": reflection.cost_usd,
    }
    if reflection.edits:
        new = await store.apply_edits(db, harness, reflection.edits)
        result["to_version"] = new.version
        result["diff"] = diff(harness, new)
    else:
        result["to_version"] = harness.version
        result["diff"] = []
    if comments:
        await db.comments.update_many(
            {"_id": {"$in": [c.id for c in comments]}},
            {"$set": {"status": "applied", "applied_in_version": result["to_version"]}},
        )
    await db.reflections.insert_one({"workspace_id": workspace_id, "created_at": datetime.now(UTC), **result})
    return result


# Chat: answers and learns in real time

_chat_locks: dict[str, asyncio.Lock] = {}


async def chat_message(
    db: AsyncDatabase,
    workspace_id: str,
    message: str,
    conversation_id: str | None = None,
    report_id: str | None = None,
    finding_id: str | None = None,
    template_id: str | None = None,
) -> dict:
    """Send one reader message. The reply may include changes the analyst learned and saved immediately."""
    ws = await store.get_workspace(db, workspace_id)
    lock = _chat_locks.setdefault(workspace_id, asyncio.Lock())
    async with lock:
        if conversation_id:
            convo = await db.conversations.find_one({"_id": conversation_id, "workspace_id": workspace_id})
            if not convo:
                raise KeyError(f"No conversation {conversation_id!r}")
            report_id = report_id or convo.get("report_id")
        if report_id:
            report_doc = await db.reports.find_one({"_id": ObjectId(report_id), "workspace_id": workspace_id})
            if not report_doc:
                raise KeyError(f"No report {report_id!r}")
        else:
            report_doc = await reports.latest(db, workspace_id)
        if not conversation_id:
            conversation_id = f"conv_{ObjectId()}"
            await db.conversations.insert_one(
                {"_id": conversation_id, "workspace_id": workspace_id,
                 "report_id": str(report_doc["_id"]) if report_doc else None, "created_at": datetime.now(UTC)}
            )  # fmt: skip

        history = await db.messages.find(
            {"conversation_id": conversation_id}, sort=[("created_at", ASCENDING)]
        ).to_list()
        message_id = f"m_{ObjectId()}"
        await db.messages.insert_one(
            {"_id": message_id, "conversation_id": conversation_id, "workspace_id": workspace_id, "role": "user",
             "text": message, "finding_id": finding_id, "template_id": template_id, "created_at": datetime.now(UTC)}
        )  # fmt: skip

        harness = await store.latest(db, workspace_id)
        pointing_at = None
        if finding_id and report_doc:
            pointing_at = next((f for f in report_doc["report"]["findings"] if f["id"] == finding_id), None)
        elif template_id:
            t = next((t for t in harness.knowledge.templates if t.id == template_id), None)
            pointing_at = {"saved_view": t.model_dump()} if t else None
        as_of = date.fromisoformat(report_doc["as_of"]) if report_doc and report_doc.get("as_of") else None

        def make_data(h):
            return CompanyData(get_client()[ws["company_db"]], h, as_of=_end_of(as_of) if as_of else None,
                               time_fields=ws.get("time_fields", {}))  # fmt: skip

        async def save(_h, edits):
            return await store.apply_edits(db, await store.latest(db, workspace_id), edits, created_by="chat")

        meter = llm.CostMeter(limit_usd=get_settings().max_usd)
        reply = await chat(harness, make_data, history, message, message_id, report_doc, pointing_at, save, meter)
        reply_id = f"m_{ObjectId()}"
        learned = [{"version": item.version, "edits": [e.model_dump() for e in item.edits]} for item in reply.learned]
        await db.messages.insert_one(
            {"_id": reply_id, "conversation_id": conversation_id, "workspace_id": workspace_id, "role": "assistant",
             "text": reply.text or "Done.", "learned": learned, "cost_usd": reply.cost_usd,
             "created_at": datetime.now(UTC)}
        )  # fmt: skip
        return {
            "conversation_id": conversation_id,
            "message_id": message_id,
            "reply_id": reply_id,
            "reply": reply.text or "Done.",
            "learned": learned,
            "harness_version": (await store.latest(db, workspace_id)).version,
            "cost_usd": reply.cost_usd,
        }


async def compare_reports(db: AsyncDatabase, report_a: str, report_b: str) -> dict:
    docs = [await db.reports.find_one({"_id": ObjectId(r)}) for r in (report_a, report_b)]
    if not all(docs):
        raise KeyError("No such report")
    if docs[0]["workspace_id"] != docs[1]["workspace_id"]:
        raise ValueError("Reports belong to different companies")
    return compare(docs[0], docs[1], await store.history(db, docs[0]["workspace_id"]))
