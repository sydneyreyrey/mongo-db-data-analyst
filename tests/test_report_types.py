from datetime import date
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from common import llm
from common.settings import get_settings

needs_atlas = pytest.mark.skipif(not get_settings().mongodb_uri, reason="MONGODB_URI not set")

FINDING = {
    "id": "orders_by_store",
    "title": "Orders by store",
    "why_it_matters": "w",
    "metric": "orders",
    "segment": {},
    "period": {"start": "2024-07-01", "end": "2024-09-30"},
    "direction": "up",
    "magnitude": 0.1,
    "pipeline": {"collection": "sales", "stages": [{"$group": {"_id": "$storeLocation", "n": {"$sum": 1}}}]},
    "chart": {
        "type": "bar",
        "x": "_id",
        "y": "n",
        "title": "Orders differ by location",
        "x_label": "Location",
        "y_label": "Orders",
    },
}


def fake_reports(titles: list[str]):
    """Each call submits a report with one finding titled from `titles`. No API calls, no cost."""
    calls = []

    async def call(role, meter=None, **params):
        calls.append({**params, "messages": list(params["messages"])})
        report = {"title": "Orders are concentrated in two locations", "summary": "s", "applied": {},
                  "focus": {"headline": "Two locations carry the business.", "detail": "They take most orders."},
                  "findings": [{**FINDING, "title": titles[len(calls) - 1]}]}  # fmt: skip
        block = SimpleNamespace(type="tool_use", id="t", name="submit_report", input=report)
        usage = llm.Usage(role=role, model="fake", input_tokens=0, output_tokens=0, cache_read_tokens=0,
                          cache_write_tokens=0, cost_usd=0.0, latency_s=0.0)  # fmt: skip
        return SimpleNamespace(content=[block], stop_reason="tool_use"), usage

    return call, calls


@pytest.fixture
async def setup(monkeypatch):
    from api import main
    from common.db import get_client

    db = get_client()[get_settings().mongodb_db + "_typestest"]
    monkeypatch.setattr(main, "get_db", lambda: db)
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://t") as client:
        yield db, client
    await get_client().drop_database(db.name)


@needs_atlas
async def test_report_types_shape_reports_and_group_them(monkeypatch, setup):
    from analyst import service

    db, client = setup
    await service.connect(db, "ws", "co_retail")

    r = await client.post("/companies/ws/report-types", json={"name": "End-of-month close",
                          "brief": "Margin, costs, and anomalies to resolve before the books close."})  # fmt: skip
    assert r.status_code == 201 and r.json()["type_id"] == "end_of_month_close"
    dup = await client.post("/companies/ws/report-types", json={"name": "End of month close", "brief": "b"})
    assert dup.json()["type_id"] == "end_of_month_close_2"
    assert (await client.post("/companies/ws/report-types", json={"name": "x", "brief": " "})).status_code == 400
    upd = await client.put("/companies/ws/report-types/end_of_month_close_2", json={"name": "Marketing",
                           "brief": "Channels, promotions, and customer satisfaction."})  # fmt: skip
    assert upd.json()["name"] == "Marketing"
    assert (await client.put("/companies/ws/report-types/general", json={"brief": "x"})).status_code == 400
    types = [t["type_id"] for t in (await client.get("/companies/ws/report-types")).json()]
    assert types == ["general", "end_of_month_close", "end_of_month_close_2"]
    assert (await client.post("/companies/ws/reports", json={"report_type": "nope"})).status_code == 404

    call, calls = fake_reports(["Close finding A", "Marketing finding", "Close finding B"])
    monkeypatch.setattr(llm, "call", call)
    sept = date(2024, 9, 30)
    a = await service.run_report(db, "ws", sept, "end_of_month_close")
    await service.run_report(db, "ws", sept, "end_of_month_close_2")
    b = await service.run_report(db, "ws", sept, "end_of_month_close")

    assert "Margin, costs, and anomalies" in calls[0]["messages"][0]["content"]
    assert "Channels, promotions" in calls[1]["messages"][0]["content"]
    third = calls[2]["messages"][0]["content"]
    assert "Close finding A" in third and "Marketing finding" not in third  # follows up within its own type
    assert a["report_type"] == {"type_id": "end_of_month_close", "name": "End-of-month close"}

    tree = (await client.get("/companies/ws/reports/tree")).json()
    close = next(t for t in tree if t["type_id"] == "end_of_month_close")
    assert close["dates"][0]["as_of"] == "2024-09-30"
    assert [x["id"] for x in close["dates"][0]["reports"]] == [str(b["_id"]), str(a["_id"])]
    assert next(t for t in tree if t["type_id"] == "general")["dates"] == []

    only_close = (await client.get("/companies/ws/reports", params={"report_type": "end_of_month_close"})).json()
    assert len(only_close) == 2 and only_close[0]["report_type"]["name"] == "End-of-month close"
    latest = (await client.get("/companies/ws/reports/latest", params={"report_type": "end_of_month_close_2"})).json()
    assert latest["report"]["findings"][0]["title"] == "Marketing finding"
    cmp = (await client.get("/reports/compare", params={"a": str(a["_id"]), "b": str(b["_id"])})).json()
    assert cmp["same_report_type"] and cmp["same_review_date"]
