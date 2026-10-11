import pytest

from analyst.data import CompanyData, QueryRejected, check_pipeline
from analyst.report import Chart, vega_lite
from common.settings import get_settings
from harness.edits import Edit, apply_edit
from harness.seed import seed_harness

COLLS = {"sales"}


@pytest.mark.parametrize(
    "stages",
    [
        [{"$out": "copy"}],
        [{"$merge": {"into": "copy"}}],
        [{"$match": {"$where": "true"}}],
        [{"$lookup": {"from": {"db": "sample_supplies", "coll": "sales"}, "as": "x"}}],
        [{"$unionWith": "other_collection"}],
        [{"$unionWith": {"coll": "sales", "pipeline": [{"$out": "x"}]}}],
        [{"$match": {}, "$limit": 1}],
        {"$match": {}},
    ],
)
def test_rejects_writes_and_cross_database_access(stages):
    with pytest.raises(QueryRejected):
        check_pipeline(stages, COLLS)


def test_allows_same_database_lookup():
    check_pipeline([{"$lookup": {"from": "sales", "localField": "a", "foreignField": "b", "as": "x"}}], COLLS)


def test_chart_types_inferred_from_rows():
    rows = [{"month": "2024-01-01T00:00:00", "revenue": 10.5, "store": "A"}]
    spec = vega_lite(Chart(type="line", x="month", y="revenue", color="store"), rows)
    assert spec["encoding"]["x"]["type"] == "temporal"
    assert spec["encoding"]["color"]["field"] == "store"
    assert spec["data"]["values"] == rows


needs_atlas = pytest.mark.skipif(not get_settings().mongodb_uri, reason="MONGODB_URI not set")


@pytest.fixture
def company_db():
    from common.db import get_client

    return get_client()["co_retail"]


@needs_atlas
async def test_exclusions_are_applied_to_every_query(company_db):
    plain = CompanyData(company_db, seed_harness("t"))
    excluded = CompanyData(
        company_db,
        apply_edit(
            seed_harness("t"),
            Edit(
                action="add_exclusion",
                target="test_accounts",
                payload={"filter": {"customer.email": {"$regex": "@test\\.example\\.com$"}}, "reason": "QA"},
                rationale="t",
                source="c",
            ),
        ),
    )
    count = [{"$count": "n"}]
    before = (await plain.aggregate("sales", count))[0]["n"]
    after = (await excluded.aggregate("sales", count))[0]["n"]
    assert before - after == 40
    assert (await excluded.list_collections())[0]["documents"] == after


@needs_atlas
async def test_scoped_to_company_database(company_db):
    data = CompanyData(company_db, seed_harness("t"))
    assert await data.collections() == {"sales"}
    with pytest.raises(QueryRejected):
        await data.aggregate("sample_supplies.sales", [])


@needs_atlas
async def test_describe_and_dates(company_db):
    data = CompanyData(company_db, seed_harness("t"))
    desc = await data.describe_collection("sales")
    fields = {f["field"]: f for f in desc["fields"]}
    assert "items[].price" in fields and "decimal" in fields["items[].price"]["types"]
    assert fields["saleDate"]["range"][0].startswith("2021")
    rows = await data.aggregate(
        "sales", [{"$match": {"saleDate": {"$gte": {"$date": "2025-01-01T00:00:00Z"}}}}, {"$count": "n"}]
    )
    assert rows[0]["n"] > 0
    top = await data.distinct_values("sales", "items[].name")
    assert top["values"][0]["value"] == "notepad"
