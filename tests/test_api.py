import pytest
from httpx import ASGITransport, AsyncClient

from common.ingest import infer_types
from common.settings import get_settings

CSV = b"""order_id,order_date,region,amount,returned
1,2024-01-05,North,"1,200.50",false
2,2024-01-09,South,300,true
3,2024-02-11,North,75.25,false
"""


def test_infer_types():
    rows = [{"a": "1", "b": "2024-01-05", "c": "x", "d": "true", "e": ""}, {"a": "2.5", "b": "01/09/2024", "c": "3",
             "d": "False", "e": ""}]  # fmt: skip
    assert infer_types(rows) == {"a": "number", "b": "date", "c": "string", "d": "bool", "e": "string"}


needs_atlas = pytest.mark.skipif(not get_settings().mongodb_uri, reason="MONGODB_URI not set")


@pytest.fixture
async def api(monkeypatch):
    from api import main
    from common.db import get_client

    test_db = get_client()[get_settings().mongodb_db + "_apitest"]
    monkeypatch.setattr(main, "get_db", lambda: test_db)
    created: list[str] = []
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://t") as client:
        yield client, created
    await get_client().drop_database(test_db.name)
    for name in created:
        await get_client().drop_database(name)


@needs_atlas
async def test_upload_csv_creates_company_with_detected_dates(api):
    client, created = api
    r = await client.post(
        "/companies/upload", files={"file": ("orders.csv", CSV, "text/csv")}, data={"name": "API Test Shop"}
    )
    assert r.status_code == 201, r.text
    ws = r.json()
    created.append(ws["company_db"])
    assert ws["company_db"] == "co_api_test_shop"
    assert ws["time_fields"] == {"orders": "order_date"}
    assert ws["loaded"]["columns"] == {
        "order_id": "number", "order_date": "date", "region": "string", "amount": "number", "returned": "bool"
    }  # fmt: skip

    wid = ws["_id"]
    assert [c["id"] for c in (await client.get("/companies")).json()] == [wid]
    assert (await client.get(f"/companies/{wid}")).json()["harness_version"] == 1

    r = await client.put(f"/companies/{wid}/time-fields", json={"time_fields": {"orders": "order_date"}})
    assert r.status_code == 200

    r = await client.post(f"/companies/{wid}/comments", json={"text": "Ignore returned orders", "kind": "comment"})
    assert r.status_code == 201 and r.json()["status"] == "pending"
    assert len((await client.get(f"/companies/{wid}/comments", params={"status": "pending"})).json()) == 1
    assert (await client.post(f"/companies/{wid}/comments", json={"text": "  "})).status_code == 400

    assert (await client.get(f"/companies/{wid}/harness")).json()["version"] == 1
    assert len((await client.get(f"/companies/{wid}/harness/versions")).json()) == 1
    diff = await client.get(f"/companies/{wid}/harness/diff", params={"from_version": 1, "to_version": 1})
    assert diff.json()["changes"] == []
    assert (await client.post(f"/companies/{wid}/harness/revert", json={"version": 9})).status_code == 404
    assert (await client.get(f"/companies/{wid}/reports/latest")).status_code == 404


@needs_atlas
async def test_connect_rules(api):
    client, _ = api
    assert (await client.post("/companies", json={"company_db": "sample_supplies"})).status_code == 400
    assert (await client.post("/companies", json={"company_db": "co_does_not_exist"})).status_code == 404
    assert (await client.get("/companies/nope")).status_code == 404
    assert (await client.get("/reports/not-an-id")).status_code == 404
    assert (await client.get("/jobs/nope")).status_code == 404
