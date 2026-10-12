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
    edit("set_report_format", "report", {"format": "Open with the month being closed."}),
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


EXCLUDE = {"lesson": "Exclude test accounts (@test.example.com).",
           "edits": [{"action": "add_exclusion", "target": "test_accounts", "rationale": "reader asked",
                      "payload": {"filter": {"customer.email": {"$regex": "@test\\.example\\.com$"}},
                                  "reason": "QA accounts"}}]}  # fmt: skip
WEEKLY = {"lesson": "Show trends weekly.",
          "edits": [{"action": "set_preference", "target": "time_grain", "rationale": "r",
                     "payload": {"value": "week"}}]}  # fmt: skip
BAD = {"lesson": "Exclude nobody.", "edits": [{"action": "add_exclusion", "target": "nothing", "rationale": "x",
       "payload": {"filter": {"customer.email": "nobody@nowhere"}, "reason": "x"}}]}  # fmt: skip


@pytest.fixture
async def memory_db():
    from common.db import get_client

    db = get_client()[get_settings().mongodb_db + "_chattest"]
    yield db
    await get_client().drop_database(db.name)


@needs_atlas
async def test_chat_keeps_lessons_in_working_memory_until_dream(monkeypatch, memory_db):
    from analyst import memory, service
    from harness import store

    db = memory_db
    await service.connect(db, "ws", "co_retail")
    call, calls = fake_llm([
        ([tool("learn", EXCLUDE)], "tool_use"),
        ([text("Got it. I will leave out @test.example.com accounts from now on.")], "end_turn"),
        ([tool("learn", BAD)], "tool_use"),
        ([text("I could not find any matching accounts, so I did not keep that.")], "end_turn"),
        ([tool("learn", WEEKLY)], "tool_use"),
        ([text("Weekly from now on.")], "end_turn"),
    ])  # fmt: skip
    monkeypatch.setattr(llm, "call", call)

    first = await service.chat_message(db, "ws", "Exclude test accounts, anything @test.example.com")
    assert first["reply"].startswith("Got it") and first["lessons"][0]["text"].startswith("Exclude test")
    assert first["harness_version"] == 1  # long-term memory unchanged until a dream
    assert (await store.latest(db, "ws")).knowledge.exclusions == []
    lesson = (await memory.working_lessons(db, "ws"))[0]
    assert lesson["edits"][0]["source"] == first["message_id"] and lesson["status"] == "working"

    second = await service.chat_message(
        db, "ws", "Also exclude nobody@nowhere", conversation_id=first["conversation_id"]
    )
    assert second["lessons"] == []
    assert "matches no documents" in calls[3]["messages"][-1]["content"][0]["content"]
    # Working memory is applied in the next conversation turn: the playbook it sees includes the exclusion.
    system_text = " ".join(b["text"] for b in calls[2]["system"])
    assert "test_accounts" in system_text and "Working memory" in system_text
    assert calls[2]["messages"][1]["content"].startswith("Got it")  # it saw the earlier exchange

    third = await service.chat_message(db, "ws", "Show me weekly from now on")
    assert third["working_memory"] == 2
    forgotten = await memory.forget(db, "ws", third["lessons"][0]["_id"])
    assert forgotten["status"] == "forgotten" and len(await memory.working_lessons(db, "ws")) == 1

    msgs = await db.messages.find({"conversation_id": first["conversation_id"]}).to_list()
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant"]


@needs_atlas
async def test_dream_consolidates_working_memory(monkeypatch, memory_db):
    from analyst import memory, service
    from harness import store

    db = memory_db
    await service.connect(db, "ws", "co_retail")
    call, _ = fake_llm([
        ([tool("learn", EXCLUDE)], "tool_use"), ([text("Noted.")], "end_turn"),
        ([tool("learn", WEEKLY)], "tool_use"), ([text("Weekly it is.")], "end_turn"),
    ])  # fmt: skip
    monkeypatch.setattr(llm, "call", call)
    a = await service.chat_message(db, "ws", "Exclude test accounts")
    b = await service.chat_message(db, "ws", "Weekly please")
    lesson_a, lesson_b = a["lessons"][0]["_id"], b["lessons"][0]["_id"]

    keep = {"action": "add_exclusion", "target": "test_accounts", "rationale": "consolidated", "source": lesson_a,
            "payload": EXCLUDE["edits"][0]["payload"]}  # fmt: skip
    missing_decision = {"edits": [keep], "decisions": [], "journal": "j"}
    final = {"edits": [keep], "journal": "I learned to ignore QA accounts. I let the weekly request go for now.",
             "decisions": [{"source": lesson_a, "outcome": "applied", "reason": "holds up in the data"},
                           {"source": lesson_b, "outcome": "dropped", "reason": "one-off"}]}  # fmt: skip
    call, calls = fake_llm(
        [([tool("dream", missing_decision)], "tool_use"), ([tool("dream", final, "t2")], "tool_use")]
    )
    monkeypatch.setattr(llm, "call", call)

    dreamt = await service.run_dream(db, "ws")
    assert "Give a decision" in calls[1]["messages"][-1]["content"][0]["content"]  # it had to fix its first try
    assert dreamt["from_version"] == 1 and dreamt["to_version"] == 2 and dreamt["journal"].startswith("I learned")
    h = await store.latest(db, "ws")
    assert h.knowledge.exclusions[0].source == lesson_a and h.preferences.time_grain == "month"
    statuses = {x["_id"]: x["status"] for x in await memory.all_lessons(db, "ws")}
    assert statuses == {lesson_a: "consolidated", lesson_b: "dropped"}
    assert (await service.memory_view(db, "ws"))["working"] == []
    assert (await service.run_dream(db, "ws"))["journal"] == "Nothing new to consolidate."


BY_STORE = {"collection": "sales", "stages": [{"$group": {"_id": "$storeLocation", "n": {"$sum": 1}}}]}
BAR = {"type": "bar", "x": "_id", "y": "n", "title": "Orders by store", "x_label": "Store", "y_label": "Orders"}
WEEKLY_LESSON = {"lesson": "Show trends weekly.", "edits": WEEKLY["edits"]}


def show(title, lesson=None):
    payload = {"title": title, "summary": f"{title} summary", "pipeline": BY_STORE, "chart": BAR}
    return {**payload, "lesson": lesson} if lesson else payload


@needs_atlas
async def test_explore_mode_only_what_lands_becomes_long_term(monkeypatch, memory_db):
    from analyst import memory, service
    from harness import store

    db = memory_db
    await service.connect(db, "ws", "co_retail")
    call, _ = fake_llm([
        ([tool("show", show("Orders by store")),
          tool("show", show("Without test accounts", EXCLUDE), "t2")], "tool_use"),
        ([text("Here are two views.")], "end_turn"),
        ([tool("show", show("Weekly view", WEEKLY_LESSON))], "tool_use"), ([text("Weekly.")], "end_turn"),
        ([tool("show", show("Weekly again", WEEKLY_LESSON))], "tool_use"), ([text("Weekly again.")], "end_turn"),
    ])  # fmt: skip
    monkeypatch.setattr(llm, "call", call)

    first = await service.chat_message(db, "ws", "Show me orders by store, and what changes without test accounts",
                                       mode="explore")  # fmt: skip
    assert first["mode"] == "explore" and len(first["steps"]) == 2 and first["lessons"] == []
    by_store, no_test = first["steps"]
    assert by_store["vega_lite"]["data"]["values"] and no_test["candidate_lesson"]["text"].startswith("Exclude test")
    assert await memory.working_lessons(db, "ws") == []  # exploring learns nothing on its own

    kept = await service.keep_step(db, "ws", by_store["_id"])
    assert kept["strength"] == 3 and kept["edits"][0]["action"] == "save_template"
    rejected = await service.reject_step(db, "ws", no_test["_id"])
    assert rejected["status"] == "rejected" and rejected["weakened_lesson"] is None

    convo = first["conversation_id"]
    second = await service.chat_message(db, "ws", "What about weekly?", conversation_id=convo)
    assert second["mode"] == "explore"  # the conversation keeps its mode
    weekly = await service.keep_step(db, "ws", second["steps"][0]["_id"])
    third = await service.chat_message(db, "ws", "Weekly again please", conversation_id=convo)
    assert third["steps"][0]["reinforced_lesson"] == weekly["_id"]
    lessons = {x["_id"]: x for x in await memory.working_lessons(db, "ws")}
    assert lessons[weekly["_id"]]["strength"] == 4  # kept (3) + came back to it (1)

    weak = await memory.remember(db, "ws", convo, "m_x", "Maybe look at coupons.",
                                 [Edit(action="add_rule", target="coupons", payload={"text": "Check coupons."},
                                       rationale="r", source="m_x")], kind="repeated")  # fmt: skip
    assert weak["strength"] == 1

    view = by_store["title"].lower().replace(" ", "_")
    final = {"journal": "Kept the store view and weekly trends.",
             "edits": [{**kept["edits"][0], "source": kept["_id"]}, {**weekly["edits"][0], "source": weekly["_id"]}],
             "decisions": [{"source": kept["_id"], "outcome": "applied", "reason": "kept by reader"},
                           {"source": weekly["_id"], "outcome": "applied", "reason": "kept and repeated"}]}  # fmt: skip
    call, calls = fake_llm([([tool("dream", final)], "tool_use")])
    monkeypatch.setattr(llm, "call", call)
    dreamt = await service.run_dream(db, "ws")
    brief = calls[0]["messages"][0]["content"]
    assert "coupons" not in brief  # weak lessons never reach the dream
    h = await store.latest(db, "ws")
    assert h.preferences.time_grain == "week" and h.knowledge.templates[0].id == view
    assert [w["_id"] for w in dreamt["waiting"]] == [weak["_id"]] and dreamt["faded"] == []

    again = await service.run_dream(db, "ws")  # nothing reinforced the weak lesson, so it fades
    assert again["journal"] == "Nothing new to consolidate." and [f["_id"] for f in again["faded"]] == [weak["_id"]]
    statuses = {x["_id"]: x["status"] for x in await memory.all_lessons(db, "ws")}
    assert statuses == {kept["_id"]: "consolidated", weekly["_id"]: "consolidated", weak["_id"]: "faded"}


def test_chat_sees_report_sections_and_chart_numbers():
    from analyst.chat import context_message

    section = {"id": "denver", "title": "Denver sales doubled in August", "why_it_matters": "About $128,000.",
               "metric": "sales", "segment": {}, "period": {}, "direction": "up",
               "chart": {"title": "Denver monthly sales", "x_label": "Month", "y_label": "Sales ($)"}}  # fmt: skip
    doc = {"as_of": "2024-09-30", "report": {"title": "t", "focus": {"headline": "h", "detail": "d"},
           "findings": [section], "objectives": []},
           "charts": {"denver": {"data": {"values": [{"m": "2024-07-01", "s": 64278},
                                                     {"m": "2024-08-01", "s": 127529}]}}}}  # fmt: skip
    text = context_message(doc, None)
    assert "127529" in text and "Denver monthly sales" in text and "About $128,000." in text


@needs_atlas
async def test_chat_records_the_queries_it_ran(monkeypatch, memory_db):
    from analyst import service

    db = memory_db
    await service.connect(db, "ws", "co_retail")
    query = {"collection": "sales", "stages": [{"$count": "orders"}]}
    call, _ = fake_llm([([tool("run_aggregate", query)], "tool_use"), ([text("There are some orders.")], "end_turn")])
    monkeypatch.setattr(llm, "call", call)
    out = await service.chat_message(db, "ws", "How many orders are there?")
    stored = await db.messages.find_one({"_id": out["reply_id"]})
    assert stored["checks"][0]["tool"] == "run_aggregate" and stored["checks"][0]["ok"]
    assert stored["checks"][0]["input"] == query
