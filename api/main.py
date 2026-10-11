"""HTTP API for the web app: companies, reports, comments, reflection, and harness history.

make api   # http://localhost:8000/docs
"""

from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Annotated, Any, Literal

from bson import ObjectId
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from pymongo import DESCENDING

from analyst import memory, service
from analyst.data import to_jsonable
from api import jobs
from common.db import get_client, get_db
from common.ingest import IngestError, load_csv, slug
from common.settings import get_settings
from harness import store
from harness.diff import diff


@asynccontextmanager
async def lifespan(app: FastAPI):
    await jobs.mark_interrupted(get_db())
    yield


app = FastAPI(title="Recursive Analyst API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in get_settings().cors_origins.split(",")],
    allow_methods=["*"],
    allow_headers=["*"],
)


def out(value: Any) -> Any:
    return to_jsonable(value)


async def workspace_or_404(workspace_id: str) -> dict:
    try:
        return await store.get_workspace(get_db(), workspace_id)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e


async def start_job(kind: str, workspace_id: str, work) -> dict:
    try:
        return out(await jobs.start(get_db(), kind, workspace_id, work))
    except jobs.Busy as e:
        raise HTTPException(409, str(e)) from e


def report_summary(doc: dict) -> dict:
    return {
        "id": str(doc["_id"]),
        "as_of": doc.get("as_of"),
        "harness_version": doc["harness_version"],
        "created_at": doc["created_at"],
        "cost_usd": doc.get("cost_usd"),
        "findings": [f["title"] for f in doc["report"]["findings"]],
    }


# Companies


class ConnectBody(BaseModel):
    company_db: str
    workspace_id: str | None = None
    name: str = ""


@app.get("/health")
async def health() -> dict:
    return {"ok": True}


@app.get("/companies")
async def list_companies() -> list[dict]:
    db = get_db()
    result = []
    for ws in await store.list_workspaces(db):
        latest = await store.latest(db, ws["_id"])
        result.append({**ws, "id": ws["_id"], "harness_version": latest.version})
    return out(result)


@app.post("/companies", status_code=201)
async def connect_company(body: ConnectBody) -> dict:
    """Connect an existing database in the cluster. Date fields are detected automatically."""
    if (
        body.company_db.startswith(("sample_", "admin", "local", "config"))
        or body.company_db == get_settings().mongodb_db
    ):
        raise HTTPException(400, f"{body.company_db!r} cannot be connected")
    try:
        ws = await service.connect(get_db(), body.workspace_id or body.company_db, body.company_db, name=body.name,
                                   source="existing")  # fmt: skip
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    return out(ws)


@app.post("/companies/upload", status_code=201)
async def upload_company(file: Annotated[UploadFile, File()], name: Annotated[str, Form()] = "") -> dict:
    """Create a new company from a CSV: load it into its own database, detect dates, start a blank harness."""
    stem = slug(Path(file.filename or "data").stem, 30)
    company_db = f"co_{slug(name, 30) if name else stem}"
    existing = set(await get_client().list_database_names())
    base, n = company_db, 2
    while company_db in existing:
        company_db, n = f"{base}_{n}", n + 1
    try:
        loaded = await load_csv(get_client(), company_db, stem, await file.read())
    except IngestError as e:
        raise HTTPException(400, str(e)) from e
    ws = await service.connect(get_db(), company_db, company_db, name=name or stem, source="csv")
    return out({**ws, "loaded": loaded})


@app.get("/companies/{workspace_id}")
async def get_company(workspace_id: str) -> dict:
    ws = await workspace_or_404(workspace_id)
    latest = await store.latest(get_db(), workspace_id)
    return out({**ws, "id": workspace_id, "harness_version": latest.version})


class TimeFieldsBody(BaseModel):
    time_fields: dict[str, str]


@app.put("/companies/{workspace_id}/time-fields")
async def set_time_fields(workspace_id: str, body: TimeFieldsBody) -> dict:
    await workspace_or_404(workspace_id)
    await store.set_time_fields(get_db(), workspace_id, body.time_fields)
    return out(await store.get_workspace(get_db(), workspace_id))


# Reports


class ReportBody(BaseModel):
    as_of: date | None = None


@app.post("/companies/{workspace_id}/reports", status_code=202)
async def run_report(workspace_id: str, body: ReportBody | None = None) -> dict:
    """Start a report with the latest harness. Poll GET /jobs/{id}; the result holds the report id."""
    await workspace_or_404(workspace_id)
    as_of = body.as_of if body else None

    async def work() -> dict:
        doc = await service.run_report(get_db(), workspace_id, as_of)
        return report_summary(doc)

    return await start_job("report", workspace_id, work)


@app.get("/companies/{workspace_id}/reports")
async def list_reports(workspace_id: str) -> list[dict]:
    docs = await get_db().reports.find({"workspace_id": workspace_id}, sort=[("created_at", DESCENDING)]).to_list()
    return out([report_summary(d) for d in docs])


@app.get("/companies/{workspace_id}/reports/latest")
async def latest_report(workspace_id: str) -> dict:
    doc = await get_db().reports.find_one({"workspace_id": workspace_id}, sort=[("created_at", DESCENDING)])
    if not doc:
        raise HTTPException(404, "No reports yet")
    return out(doc)


async def get_report(report_id: str) -> dict:
    if not ObjectId.is_valid(report_id):
        raise HTTPException(404, "No such report")
    doc = await get_db().reports.find_one({"_id": ObjectId(report_id)})
    if not doc:
        raise HTTPException(404, "No such report")
    return out(doc)


# Comments and reflection


class CommentBody(BaseModel):
    text: str
    kind: Literal["comment", "keep", "stop"] = "comment"
    finding_id: str | None = None
    template_id: str | None = None
    report_id: str | None = None


@app.post("/companies/{workspace_id}/comments", status_code=201)
async def add_comment(workspace_id: str, body: CommentBody) -> dict:
    """Add a comment. `keep` with a finding_id saves that view; `stop` with a template_id turns a view off."""
    await workspace_or_404(workspace_id)
    if not body.text.strip():
        raise HTTPException(400, "Comment text is empty")
    return out(await service.add_comment(get_db(), workspace_id, **body.model_dump()))


@app.get("/companies/{workspace_id}/comments")
async def list_comments(workspace_id: str, status: Literal["pending", "applied"] | None = None) -> list[dict]:
    return out(await service.list_comments(get_db(), workspace_id, status))


@app.post("/companies/{workspace_id}/reflect", status_code=202)
async def run_reflect(workspace_id: str) -> dict:
    """Turn pending comments into harness edits. Poll GET /jobs/{id}; the result holds edits and the diff."""
    await workspace_or_404(workspace_id)
    return await start_job("reflect", workspace_id, lambda: service.run_reflect(get_db(), workspace_id))


@app.get("/companies/{workspace_id}/reflections")
async def list_reflections(workspace_id: str) -> list[dict]:
    docs = await get_db().reflections.find({"workspace_id": workspace_id}, sort=[("created_at", DESCENDING)]).to_list()
    return out(docs)


# Chat: the analyst answers and learns as you talk


class ChatBody(BaseModel):
    message: str
    mode: Literal["chat", "explore"] = "chat"
    conversation_id: str | None = None
    report_id: str | None = None
    finding_id: str | None = None
    template_id: str | None = None


@app.post("/companies/{workspace_id}/chat")
async def chat(workspace_id: str, body: ChatBody) -> dict:
    """Send a message about a report. Costs money. Waits for the reply (usually 5 to 40 seconds).

    The reply may include `lessons`: kept in working memory, applied at once in every conversation, and made
    permanent at the next dream. Each can be forgotten before then.
    """
    await workspace_or_404(workspace_id)
    if not body.message.strip():
        raise HTTPException(400, "Message is empty")
    running = {"workspace_id": workspace_id, "status": "running", "kind": {"$in": ["reflect", "dream"]}}
    if await get_db().jobs.find_one(running):
        raise HTTPException(409, "The analyst is reflecting or dreaming; try again when it finishes")
    try:
        return out(await service.chat_message(get_db(), workspace_id, **body.model_dump()))
    except KeyError as e:
        raise HTTPException(404, str(e)) from e


@app.get("/companies/{workspace_id}/conversations")
async def list_conversations(workspace_id: str) -> list[dict]:
    await workspace_or_404(workspace_id)
    docs = (
        await get_db().conversations.find({"workspace_id": workspace_id}, sort=[("created_at", DESCENDING)]).to_list()
    )
    return out(docs)


@app.get("/conversations/{conversation_id}/steps")
async def list_steps(conversation_id: str) -> list[dict]:
    """Exploration steps in a conversation, oldest first, with charts and whether each was kept. Free."""
    if not await get_db().conversations.find_one({"_id": conversation_id}):
        raise HTTPException(404, "No such conversation")
    docs = await get_db().explorations.find({"conversation_id": conversation_id}, sort=[("created_at", 1)]).to_list()
    return out(docs)


@app.post("/companies/{workspace_id}/explorations/{step_id}/keep")
async def keep_step(workspace_id: str, step_id: str) -> dict:
    """ "Remember this": the step becomes a strong lesson in working memory, made permanent at the next dream. Free."""
    await workspace_or_404(workspace_id)
    try:
        return out(await service.keep_step(get_db(), workspace_id, step_id))
    except KeyError as e:
        raise HTTPException(404, str(e)) from e


@app.post("/companies/{workspace_id}/explorations/{step_id}/reject")
async def reject_step(workspace_id: str, step_id: str) -> dict:
    """ "Not useful": marks the step and weakens a matching lesson in working memory. Free."""
    await workspace_or_404(workspace_id)
    try:
        return out(await service.reject_step(get_db(), workspace_id, step_id))
    except KeyError as e:
        raise HTTPException(404, str(e)) from e


@app.get("/conversations/{conversation_id}/messages")
async def list_messages(conversation_id: str) -> list[dict]:
    if not await get_db().conversations.find_one({"_id": conversation_id}):
        raise HTTPException(404, "No such conversation")
    docs = await get_db().messages.find({"conversation_id": conversation_id}, sort=[("created_at", 1)]).to_list()
    return out(docs)


# Memory: working memory (lessons from chat) and dreaming (making them permanent)


@app.get("/companies/{workspace_id}/memory")
async def get_memory(workspace_id: str) -> dict:
    """Working memory (lessons waiting for a dream), a long-term memory summary, and the last dream. Free."""
    await workspace_or_404(workspace_id)
    return out(await service.memory_view(get_db(), workspace_id))


@app.get("/companies/{workspace_id}/memory/lessons")
async def list_lessons(workspace_id: str) -> list[dict]:
    """Every lesson ever learned for this company, with what happened to it. Free."""
    await workspace_or_404(workspace_id)
    return out(await memory.all_lessons(get_db(), workspace_id))


@app.delete("/companies/{workspace_id}/memory/{lesson_id}")
async def forget_lesson(workspace_id: str, lesson_id: str) -> dict:
    """Forget a lesson before it is made permanent. Free."""
    await workspace_or_404(workspace_id)
    try:
        return out(await memory.forget(get_db(), workspace_id, lesson_id))
    except KeyError as e:
        raise HTTPException(404, str(e)) from e


@app.post("/companies/{workspace_id}/dream", status_code=202)
async def run_dream(workspace_id: str) -> dict:
    """Consolidate working memory into long-term memory. Costs money. Poll GET /jobs/{id}; result is the dream."""
    await workspace_or_404(workspace_id)
    return await start_job("dream", workspace_id, lambda: service.run_dream(get_db(), workspace_id))


@app.get("/companies/{workspace_id}/dreams")
async def list_dreams(workspace_id: str) -> list[dict]:
    """Every dream, newest first: journal, decisions per lesson, edits, and the version it produced. Free."""
    await workspace_or_404(workspace_id)
    docs = await get_db().dreams.find({"workspace_id": workspace_id}, sort=[("created_at", DESCENDING)]).to_list()
    return out(docs)


# Report comparison


@app.get("/reports/compare")
async def compare_reports(a: str, b: str) -> dict:
    """What the notebook learned between two reports of the same company, and how the findings changed. Free."""
    if not (ObjectId.is_valid(a) and ObjectId.is_valid(b)):
        raise HTTPException(404, "No such report")
    try:
        return out(await service.compare_reports(get_db(), a, b))
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


app.get("/reports/{report_id}")(get_report)  # registered after /reports/compare so that route wins


# Harness


@app.get("/companies/{workspace_id}/harness")
async def get_harness(workspace_id: str, version: int | None = None) -> dict:
    await workspace_or_404(workspace_id)
    try:
        h = await (store.get(get_db(), workspace_id, version) if version else store.latest(get_db(), workspace_id))
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    return h.model_dump()


@app.get("/companies/{workspace_id}/harness/versions")
async def harness_versions(workspace_id: str) -> list[dict]:
    await workspace_or_404(workspace_id)
    return out(await store.history(get_db(), workspace_id))


@app.get("/companies/{workspace_id}/harness/diff")
async def harness_diff(workspace_id: str, from_version: int, to_version: int) -> dict:
    await workspace_or_404(workspace_id)
    try:
        a = await store.get(get_db(), workspace_id, from_version)
        b = await store.get(get_db(), workspace_id, to_version)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    return {"from": from_version, "to": to_version, "changes": diff(a, b)}


class UndoBody(BaseModel):
    version: int
    edit_index: int


@app.post("/companies/{workspace_id}/harness/undo")
async def undo_change(workspace_id: str, body: UndoBody) -> dict:
    """Undo one learned change (edit `edit_index` of version `version`). Saved as a new version. Free."""
    await workspace_or_404(workspace_id)
    try:
        h, edit = await store.undo(get_db(), workspace_id, body.version, body.edit_index)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    return {"harness": h.model_dump(), "undone": edit.model_dump()}


class RevertBody(BaseModel):
    version: int


@app.post("/companies/{workspace_id}/harness/revert")
async def revert_harness(workspace_id: str, body: RevertBody) -> dict:
    """Make an earlier version current again (saved as a new version, so history is kept)."""
    await workspace_or_404(workspace_id)
    try:
        h = await store.revert_to(get_db(), workspace_id, body.version)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    return h.model_dump()


# Jobs


@app.get("/jobs/{job_id}")
async def get_job(job_id: str) -> dict:
    job = await get_db().jobs.find_one({"_id": job_id})
    if not job:
        raise HTTPException(404, "No such job")
    return out(job)


# Simulations (scored runs written by scripts/simulate.py)


@app.get("/simulations")
async def list_simulations() -> list[dict]:
    """Every scored run: company, run label, quarters scored per mode."""
    rows = await (
        await get_db().scorecards.aggregate(
            [
                {"$match": {"stage": "review"}},
                {
                    "$group": {
                        "_id": {"company": "$company", "run": "$run", "mode": "$mode"},
                        "quarters": {"$sum": 1},
                        "last": {"$max": "$created_at"},
                    }
                },  # fmt: skip
                {"$sort": {"last": -1}},
            ]
        )
    ).to_list()
    return out([{**r["_id"], "quarters": r["quarters"], "updated_at": r["last"]} for r in rows])


@app.get("/simulations/{company}/{run}")
async def get_simulation(company: str, run: str) -> dict:
    """Scores by quarter for each mode, plus what each round of feedback changed and whether it was kept."""
    db = get_db()
    cards = await db.scorecards.find(
        {"company": company, "run": run, "stage": "review"}, {"_id": 0}, sort=[("cycle", 1)]
    ).to_list()
    if not cards:
        raise HTTPException(404, "No such simulation")
    cycles = await db.cycles.find({"run": run, "workspace_id": f"{company}_sim_feedback_{run}"}, {"_id": 0},
                                  sort=[("cycle", 1)]).to_list()  # fmt: skip
    return out({"company": company, "run": run, "scorecards": cards, "feedback_rounds": cycles})
