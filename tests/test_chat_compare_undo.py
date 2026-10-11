from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from analyst.compare import compare
from common import llm
from common.settings import get_settings
from harness.edits import Edit, apply_edit
from harness.seed import seed_harness
from harness.undo import undo_edit

TEMPLATE = {
    "name": "Weekly count",
    "description": "Orders per week",
    "pipeline": {"collection": "sales", "stages": [{"$count": "n"}]},
    "chart": {"type": "bar", "x": "n", "y": "n"},
}
OBJECTIVE = {
    "text": "Margin 46%",
    "metric": "margin %",
    "target": 46,
    "direction": "at_least",
    "deadline": "2024-12-31",
}


def edit(action, target, payload=None):
    return Edit(action=action, target=target, payload=payload or {}, rationale="r", source="s")


def base():
    h = seed_harness("t")
    for e in [
        edit("set_vocab", "aov", {"definition": "old"}),
        edit("add_rule", "r1", {"text": "old rule"}),
        edit("set_focus_metric", "revenue", {"definition": "old"}),
        edit("set_objective", "o1", OBJECTIVE),
        edit("save_template", "t1", TEMPLATE),
        edit("enable_tool", "forecast"),
    ]:
        h = apply_edit(h, e)
    return h


CHANGES = [
    edit("set_vocab", "aov", {"definition": "new"}),
    edit("set_vocab", "margin", {"definition": "new term"}),
    edit("remove_vocab", "aov"),
    edit("add_rule", "r2", {"text": "new rule"}),
    edit("retire_rule", "r1"),
    edit("set_focus_metric", "revenue", {"definition": "new"}),
    edit("add_exclusion", "x", {"filter": {"a": 1}, "reason": "r"}),
    edit("set_preference", "time_grain", {"value": "week"}),
    edit("set_objective", "o1", {**OBJECTIVE, "target": 50}),
    edit("retire_objective", "o1"),
    edit("set_template_status", "t1", {"status": "off"}),
    edit("save_template", "t2", TEMPLATE),
    edit("disable_tool", "forecast"),
    edit("enable_tool", "detect_spikes"),
    edit("annotate_collection", "sales", {"note": "n"}),
]


@pytest.mark.parametrize("change", CHANGES, ids=lambda e: f"{e.action}:{e.target}")
def test_undo_restores_exactly_the_touched_entry(change):
    before = base()
    after = apply_edit(before, change)
    # An unrelated later change must survive the undo.
    later = apply_edit(after, edit("add_rule", "unrelated", {"text": "keep me"}))
    restored = undo_edit(later, before, change)
    expected = apply_edit(before, edit("add_rule", "unrelated", {"text": "keep me"})).model_dump()
    got = restored.model_dump()
    for d in (expected, got):
        d.pop("version"), d.pop("parent_version")
        d["knowledge"]["rules"].sort(key=lambda r: r["id"])
        for key in ("objectives", "templates", "focus_metrics"):
            d["knowledge"][key].sort(key=lambda x: x.get("id") or x.get("name"))
        d["capabilities"]["tools_enabled"].sort()
    assert got == expected
    assert restored.version == later.version + 1


def report_doc(rid, version, findings, objectives=(), as_of="2024-09-30", minute=0):
    return {
        "_id": rid,
        "as_of": as_of,
        "harness_version": version,
        "created_at": datetime(2026, 1, 1, 0, minute, tzinfo=UTC),
        "cost_usd": 0.1,
        "report": {"summary": "s", "findings": findings, "objectives": list(objectives), "applied": {}},
        "template_runs": [],
    }


def f(fid, metric, segment, magnitude=-0.5):
    return {"id": fid, "title": fid, "metric": metric, "segment": segment, "period": {}, "direction": "down",
            "magnitude": magnitude}  # fmt: skip


def test_compare_pairs_findings_and_lists_notebook_changes():
    old = report_doc("a", 1, [f("denver_spike", "revenue", {"store": "Denver"}), f("x", "units", {})],
                     [{"objective_id": "o1", "value": 44, "target": 46, "status": "off_track"}])  # fmt: skip
    new = report_doc("b", 3, [f("denver_margin", "revenue", {"store": "denver"}, -0.2), f("seattle", "margin", {})],
                     [{"objective_id": "o1", "value": 46.5, "target": 46, "status": "on_track"}], minute=5)  # fmt: skip
    versions = [
        {"version": 1, "created_by": "seed", "created_at": 1, "edits": []},
        {"version": 2, "created_by": "chat", "created_at": 2, "edits": [{"action": "add_exclusion"}]},
        {"version": 3, "created_by": "reflect", "created_at": 3, "edits": [{"action": "set_preference"}]},
    ]
    out = compare(new, old, versions)  # order of arguments does not matter
    assert out["before"]["id"] == "a" and out["same_review_date"]
    assert [c["version"] for c in out["notebook_changes"]] == [2, 3]
    pair = out["findings"]["in_both"][0]
    assert pair["before"]["id"] == "denver_spike" and pair["after"]["magnitude"] == -0.2
    assert [x["id"] for x in out["findings"]["new"]] == ["seattle"]
    assert [x["id"] for x in out["findings"]["dropped"]] == ["x"]
    assert out["objectives"][0]["after"]["status"] == "on_track"


needs_atlas = pytest.mark.skipif(not get_settings().mongodb_uri, reason="MONGODB_URI not set")


def fake_llm(script):
    """Stand in for the model: return scripted responses in order. No API calls, no cost."""
    calls = []

    async def call(role, meter=None, **params):
        calls.append({**params, "messages": list(params["messages"])})
        blocks, stop = script[len(calls) - 1]
        usage = llm.Usage(role=role, model="fake", input_tokens=0, output_tokens=0, cache_read_tokens=0,
                          cache_write_tokens=0, cost_usd=0.0, latency_s=0.0)  # fmt: skip
        return SimpleNamespace(content=blocks, stop_reason=stop), usage

    return call, calls


def tool(name, payload, tid="t1"):
    return SimpleNamespace(type="tool_use", id=tid, name=name, input=payload)


def text(t):
    return SimpleNamespace(type="text", text=t)


EXCLUDE = {"edits": [{"action": "add_exclusion", "target": "test_accounts", "rationale": "reader asked",
                      "payload": {"filter": {"customer.email": {"$regex": "@test\\.example\\.com$"}},
                                  "reason": "QA accounts"}}]}  # fmt: skip
BAD = {"edits": [{"action": "add_exclusion", "target": "nothing", "rationale": "x",
                  "payload": {"filter": {"customer.email": "nobody@nowhere"}, "reason": "x"}}]}  # fmt: skip


@pytest.fixture
async def memory_db():
    from common.db import get_client

    db = get_client()[get_settings().mongodb_db + "_chattest"]
    yield db
    await get_client().drop_database(db.name)


@needs_atlas
async def test_chat_learns_stores_and_undoes(monkeypatch, memory_db):
    from analyst import service
    from harness import store

    db = memory_db
    await service.connect(db, "ws", "co_retail")
    call, calls = fake_llm([
        ([tool("learn", EXCLUDE)], "tool_use"),
        ([text("Got it. I will leave out @test.example.com accounts from now on.")], "end_turn"),
        ([tool("learn", BAD)], "tool_use"),
        ([text("I could not find any matching accounts, so I did not change anything.")], "end_turn"),
    ])  # fmt: skip
    monkeypatch.setattr(llm, "call", call)

    first = await service.chat_message(db, "ws", "Exclude test accounts, anything @test.example.com")
    assert first["reply"].startswith("Got it") and first["harness_version"] == 2
    assert first["learned"][0]["edits"][0]["source"] == first["message_id"]
    h = await store.latest(db, "ws")
    assert h.knowledge.exclusions[0].id == "test_accounts"

    second = await service.chat_message(
        db, "ws", "Also exclude nobody@nowhere", conversation_id=first["conversation_id"]
    )
    assert second["learned"] == [] and second["harness_version"] == 2
    assert "matches no documents" in calls[3]["messages"][-1]["content"][0]["content"]
    # The second turn saw the first exchange as history.
    assert calls[2]["messages"][1]["content"].startswith("Got it")

    msgs = await db.messages.find({"conversation_id": first["conversation_id"]}).to_list()
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant"]

    restored, undone = await store.undo(db, "ws", 2, 0)
    assert undone.target == "test_accounts" and restored.knowledge.exclusions == [] and restored.version == 3
