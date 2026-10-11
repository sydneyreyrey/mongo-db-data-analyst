"""Run a company's quarterly review timeline, scored, with and without manager feedback.

    make simulate COMPANY=co_retail RUN=r1           # feedback and baseline side by side
    make simulate COMPANY=co_retail RUN=r1 MODE=feedback

Each mode gets its own fresh workspace (`<company>_sim_<mode>_<run>`), so runs never share a harness.
Everything is stored in MongoDB: reports, comments, harness versions, `scorecards`, and `cycles`.
"""

import argparse
import asyncio
from datetime import UTC, date, datetime

from pymongo.asynchronous.database import AsyncDatabase

from analyst import service
from common.db import get_client, get_db
from harness import store
from scorekeeper.score import Scorecard, load_key, score_report

COLLECTIONS = ("workspaces", "harness_versions", "reports", "comments", "reflections", "jobs")


async def reset(db: AsyncDatabase, workspace_id: str) -> None:
    for name in COLLECTIONS:
        key = "_id" if name == "workspaces" else "workspace_id"
        await db[name].delete_many({key: workspace_id})
    await db.scorecards.delete_many({"workspace_id": workspace_id})
    await db.cycles.delete_many({"workspace_id": workspace_id})


async def score(db: AsyncDatabase, ws: dict, company: str, report_doc: dict, cycle: int) -> Scorecard:
    harness = await store.get(db, ws["_id"], report_doc["harness_version"])
    return await score_report(get_client()[company], company, report_doc, harness, cycle, ws["time_fields"])


async def save_card(db: AsyncDatabase, run: str, mode: str, ws_id: str, stage: str, card: Scorecard) -> None:
    await db.scorecards.insert_one(
        {
            "run": run,
            "mode": mode,
            "workspace_id": ws_id,
            "stage": stage,
            "created_at": datetime.now(UTC),
            **card.model_dump(),
        }  # fmt: skip
    )


def target(comment: dict, report_doc: dict, harness) -> dict:
    """Point keep and stop comments at the finding or saved view they are about, like a click in the UI."""
    check = comment.get("check") or {}
    keyword = check.get("keyword", "")
    if comment["kind"] == "template_keep":
        hit = next(
            (f for f in report_doc["report"]["findings"] if keyword in f"{f['metric']} {f['title']}".lower()), None
        )
        return {"kind": "keep", "finding_id": hit["id"]} if hit else {"kind": "comment"}
    if comment["kind"] == "template_stop":
        hit = next((t for t in harness.knowledge.templates if keyword in f"{t.name} {t.description}".lower()), None)
        return {"kind": "stop", "template_id": hit.id} if hit else {"kind": "comment"}
    return {"kind": "comment"}


def line(mode: str, as_of: date, card: Scorecard, extra: str = "") -> str:
    parts = " ".join(
        f"{k}={getattr(card, k).value:.2f}" if getattr(card, k).value is not None else f"{k}=-"
        for k in ("recall", "compliance", "objectives", "reproducibility")
    )
    return f"[{mode:8}] {as_of} v{card.harness_version:<2} total={card.total:.2f}  {parts} {extra}"


async def run_timeline(company: str, run: str, mode: str, resume: bool = False) -> None:
    """Run the timeline. With `resume`, reuse every saved review and reflection and only pay for what is missing."""
    db = get_db()
    ws_id = f"{company}_sim_{mode}_{run}"
    if not resume:
        await reset(db, ws_id)
    ws = await service.connect(db, ws_id, company, name=f"{company} simulation ({mode}, {run})", source="simulation")
    _, cycles = load_key(company)
    finished_rounds = {c["cycle"] for c in await db.cycles.find({"workspace_id": ws_id}).to_list()}

    for i, cycle in enumerate(cycles):
        as_of = date.fromisoformat(cycle["as_of"])
        if i in finished_rounds:
            continue
        saved = await db.scorecards.find_one({"workspace_id": ws_id, "stage": "review", "as_of": cycle["as_of"]})
        if saved:
            doc = await db.reports.find_one(
                {"workspace_id": ws_id, "as_of": cycle["as_of"], "harness_version": saved["harness_version"]},
                sort=[("created_at", 1)],
            )
            print(f"[{mode:8}] {as_of} review already scored ({saved['total']:.2f}), reusing it", flush=True)
        else:
            doc = await service.run_report(db, ws_id, as_of)
            card = await score(db, ws, company, doc, i)
            await save_card(db, run, mode, ws_id, "review", card)
            print(line(mode, as_of, card, f"${doc['cost_usd']:.2f}"), flush=True)
        if mode == "baseline" or not cycle["comments"]:
            continue

        # Manager feedback, then reflect into a new harness version.
        before = await store.get(db, ws_id, doc["harness_version"])
        posted = {c["_id"] for c in await db.comments.find({"workspace_id": ws_id}, {"_id": 1}).to_list()}
        for c in cycle["comments"]:
            if f"{ws_id}:{c['id']}" in posted:
                continue
            await service.add_comment(db, ws_id, c["text"], comment_id=f"{ws_id}:{c['id']}", report_id=str(doc["_id"]),
                                      **target(c, doc, before))  # fmt: skip
        earlier = await db.reflections.find_one(
            {"workspace_id": ws_id, "created_at": {"$gt": doc["created_at"]}}, sort=[("created_at", 1)]
        )
        pending = await db.comments.find_one({"workspace_id": ws_id, "status": "pending"})
        if earlier and not pending:
            reflection = {k: earlier[k] for k in ("from_version", "to_version", "edits", "notes", "diff", "cost_usd")}
            print(f"[{mode:8}] {as_of} reflection already done, reusing it", flush=True)
        else:
            reflection = await service.run_reflect(db, ws_id)
        record = {"run": run, "mode": mode, "workspace_id": ws_id, "cycle": i, "as_of": cycle["as_of"],
                  "comments": [c["id"] for c in cycle["comments"]], "reflection": reflection}  # fmt: skip

        # Keep or revert: redo the same quarter with the new harness; score both against the new comments too.
        if reflection["edits"]:
            redo = await service.run_report(db, ws_id, as_of)
            old = await score(db, ws, company, doc, i + 1)
            new = await score(db, ws, company, redo, i + 1)
            await save_card(db, run, mode, ws_id, "check_before", old)
            await save_card(db, run, mode, ws_id, "check_after", new)
            kept = new.total >= old.total
            if not kept:
                await store.revert_to(db, ws_id, before.version)
            record.update(before_total=old.total, after_total=new.total, kept=kept)
            verdict = "kept" if kept else "REVERTED"
            change = f"v{before.version}->v{reflection['to_version']} {len(reflection['edits'])} edits"
            print(f"[{mode:8}] {as_of} reflect {change}: {old.total:.2f} -> {new.total:.2f} {verdict}", flush=True)
        await db.cycles.insert_one({**record, "created_at": datetime.now(UTC)})

    cost = sum([d["cost_usd"] async for d in db.reports.find({"workspace_id": ws_id}, {"cost_usd": 1})])
    cost += sum([r["cost_usd"] async for r in db.reflections.find({"workspace_id": ws_id}, {"cost_usd": 1})])
    print(f"[{mode:8}] done, total cost ${cost:.2f}", flush=True)


async def main(company: str, run: str, modes: list[str], resume: bool) -> None:
    await asyncio.gather(*(run_timeline(company, run, m, resume) for m in modes))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--company", required=True)
    parser.add_argument("--run", default="r1")
    parser.add_argument("--mode", choices=["feedback", "baseline", "both"], default="both")
    parser.add_argument("--resume", action="store_true", help="Continue an interrupted run without redoing work")
    args = parser.parse_args()
    modes = ["feedback", "baseline"] if args.mode == "both" else [args.mode]
    asyncio.run(main(args.company, args.run, modes, args.resume))
