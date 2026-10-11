import json
import re
from datetime import date, datetime
from pathlib import Path

import pytest

from analyst.agent import _validate_report, kickoff, run_templates
from analyst.data import CompanyData
from analyst.prompt_builder import BASE_INSTRUCTIONS
from analyst.report import Report
from analyst.tools import TOOLS
from common.settings import get_settings
from harness.edits import Edit, apply_edit
from harness.seed import seed_harness

ROOT = Path(__file__).resolve().parent.parent
BUSINESS_WORDS = [
    "store", "sale", "retail", "laptop", "coupon", "supplies", "movie", "film", "rental", "airbnb",
    "listing", "seattle", "denver", "london", "austin", "satisfaction",
]  # fmt: skip


def test_base_instructions_are_business_neutral():
    texts = {
        "base instructions": BASE_INSTRUCTIONS,
        "kickoff": kickoff("co_x", date(2024, 1, 1), [], []),
        "tools": json.dumps(TOOLS),
        "seed harness": seed_harness("ws").model_dump_json(),
    }
    hits = {
        name: word
        for name, text in texts.items()
        for word in BUSINESS_WORDS
        if re.search(rf"\b{word}", text, re.IGNORECASE)
    }
    assert not hits, hits


def test_timeline_is_well_formed():
    timeline = json.loads((ROOT / "bench/co_retail/timeline.json").read_text())
    planted = {f["id"] for f in json.loads((ROOT / "bench/co_retail/planted.json").read_text())["findings"]}
    dates = [c["as_of"] for c in timeline["cycles"]]
    assert dates == sorted(dates) and len(dates) == 9
    ids = [m["id"] for c in timeline["cycles"] for m in c["comments"]]
    assert len(ids) == len(set(ids))
    known = {"focus_metric", "suppress_story", "objective", "preference", "exclusion", "template_status", "ordering"}
    for c in timeline["cycles"]:
        for m in c["comments"]:
            check = m["check"]
            if check is None:
                continue
            assert check["type"] in known, m["id"]
            if check["type"] == "suppress_story":
                assert check["story_id"] in planted
            if check["type"] == "objective":
                assert check["reference"]["stages"] and check["scored_until"]


needs_atlas = pytest.mark.skipif(not get_settings().mongodb_uri, reason="MONGODB_URI not set")


def company(harness=None, as_of: datetime | None = None) -> CompanyData:
    from common.db import get_client

    return CompanyData(
        get_client()["co_retail"],
        harness or seed_harness("t"),
        as_of=as_of,
        time_fields={"sales": "saleDate"},
    )


AS_OF = datetime(2023, 12, 31, 23, 59, 59)


@needs_atlas
async def test_as_of_hides_the_future():
    past, everything = company(as_of=AS_OF), company()
    latest = [{"$group": {"_id": None, "max": {"$max": "$saleDate"}, "n": {"$sum": 1}}}]
    p = (await past.aggregate("sales", latest))[0]
    e = (await everything.aggregate("sales", latest))[0]
    assert p["max"] <= AS_OF.isoformat() and e["max"] > AS_OF.isoformat()
    assert p["n"] < e["n"]
    desc = await past.describe_collection("sales")
    assert next(f for f in desc["fields"] if f["field"] == "saleDate")["range"][1] <= AS_OF.isoformat()
    assert (await past.list_collections())[0]["documents"] == p["n"]


@needs_atlas
async def test_as_of_applies_inside_union_and_lookup():
    past = company(as_of=AS_OF)
    n = (await past.aggregate("sales", [{"$count": "n"}]))[0]["n"]
    union = await past.aggregate("sales", [{"$unionWith": "sales"}, {"$count": "n"}])
    assert union[0]["n"] == 2 * n
    joined = await past.aggregate(
        "sales",
        [
            {"$limit": 1},
            {"$lookup": {"from": "sales", "pipeline": [{"$match": {}}], "as": "all"}},
            {"$project": {"k": {"$size": "$all"}, "latest": {"$max": "$all.saleDate"}}},
        ],
    )
    assert joined[0]["k"] == n and joined[0]["latest"] <= AS_OF.isoformat()


MARGIN_STAGES = [
    {"$unwind": "$items"},
    {
        "$group": {
            "_id": None,
            "rev": {"$sum": {"$multiply": [{"$toDouble": "$items.price"}, "$items.quantity"]}},
            "cost": {"$sum": {"$multiply": [{"$toDouble": "$items.cost"}, "$items.quantity"]}},
        }
    },
    {"$project": {"_id": 0, "value": {"$multiply": [100, {"$divide": [{"$subtract": ["$rev", "$cost"]}, "$rev"]}]}}},
]


def with_objective():
    return apply_edit(
        seed_harness("t"),
        Edit(
            action="set_objective",
            target="o1",
            payload={
                "text": "margin",
                "metric": "gross margin %",
                "target": 46,
                "direction": "at_least",
                "deadline": "2024-12-31",
            },  # fmt: skip
            rationale="t",
            source="o1",
        ),
    )


def report_with(value: float | None) -> dict:
    objectives = []
    if value is not None:
        objectives = [
            {
                "objective_id": "o1",
                "value": value,
                "target": 46,
                "status": "on_track",
                "note": "n",
                "pipeline": {"collection": "sales", "stages": MARGIN_STAGES},
            }  # fmt: skip
        ]
    return Report(summary="s", findings=[], objectives=objectives, applied={}).model_dump()


@needs_atlas
async def test_objective_status_must_match_its_pipeline():
    harness = with_objective()
    data = company(harness, as_of=AS_OF)
    true_value = (await data.aggregate("sales", MARGIN_STAGES))[0]["value"]
    report, _ = await _validate_report(data, harness, report_with(true_value))
    assert report.objectives[0].value == true_value
    with pytest.raises(ValueError, match="stated value"):
        await _validate_report(data, harness, report_with(true_value + 3))
    with pytest.raises(ValueError, match="missing status"):
        await _validate_report(data, harness, report_with(None))


@needs_atlas
async def test_templates_run_only_when_on():
    template = {
        "name": "Monthly count",
        "description": "Orders per month",
        "pipeline": {
            "collection": "sales",
            "stages": [
                {"$group": {"_id": {"$dateToString": {"format": "%Y-%m", "date": "$saleDate"}}, "n": {"$sum": 1}}},
                {"$sort": {"_id": 1}},
            ],
        },  # fmt: skip
        "chart": {"type": "line", "x": "_id", "y": "n"},
    }
    h = apply_edit(seed_harness("t"), Edit(action="save_template", target="t1", payload=template, rationale="r",
                                           source="c"))  # fmt: skip
    runs, charts, texts = await run_templates(company(h, as_of=AS_OF), h.knowledge.templates)
    assert runs[0].ok and "t1" in charts and charts["t1"]["data"]["values"][-1]["_id"] == "2023-12"
    off = apply_edit(h, Edit(action="set_template_status", target="t1", payload={"status": "off"}, rationale="r",
                             source="c2"))  # fmt: skip
    assert await run_templates(company(off, as_of=AS_OF), off.knowledge.templates) == ([], {}, [])
