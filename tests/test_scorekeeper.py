from datetime import date

import pytest

from common.settings import get_settings
from harness.edits import Edit, apply_edit
from harness.seed import seed_harness
from scorekeeper.score import load_key, matches, quarter_start, reference_value, score_report

PLANTED, CYCLES = load_key("co_retail")
STORY = {s["id"]: s for s in PLANTED}


def finding(**kw):
    base = {
        "id": "f1",
        "title": "Seattle revenue fell in Q2",
        "why_it_matters": "",
        "metric": "revenue",
        "segment": {"storeLocation": "Seattle"},
        "period": {"start": "2024-04-01", "end": "2024-06-30"},
        "direction": "down",
        "magnitude": -0.5,
        "pipeline": {"collection": "sales", "stages": [{"$count": "n"}]},
        "chart": {"type": "bar", "x": "n", "y": "n"},
    }
    return {**base, **kw}


@pytest.mark.parametrize(
    ("f", "expected"),
    [
        (finding(), True),
        (finding(metric="weekly margin", title="Seattle margin slump"), True),
        (finding(segment={"storeLocation": "Denver"}, title="Denver revenue fell"), False),
        (finding(direction="up"), False),
        (finding(period={"start": "2024-08-01", "end": "2024-09-30"}), False),
        (finding(period={"start": "not a date", "end": "2024-06-30"}), False),
    ],
)
def test_seattle_matching(f, expected):
    assert matches(f, STORY["seattle_revenue_drop"]) is expected


def test_multi_term_story_needs_every_term():
    austin = STORY["austin_laptop_surge"]
    period = {"start": "2025-07-01", "end": "2025-12-31"}
    hit = finding(title="Austin laptop units doubled", metric="units", direction="up", period=period,
                  segment={"storeLocation": "Austin"})  # fmt: skip
    miss = finding(title="Austin units doubled", metric="units", direction="up", period=period,
                   segment={"storeLocation": "Austin"})  # fmt: skip
    assert matches(hit, austin) and not matches(miss, austin)


def test_quarter_start():
    assert quarter_start(date(2024, 9, 30)) == date(2024, 7, 1)
    assert quarter_start(date(2025, 1, 1)) == date(2025, 1, 1)


needs_atlas = pytest.mark.skipif(not get_settings().mongodb_uri, reason="MONGODB_URI not set")
TIME_FIELDS = {"sales": "saleDate"}
Q3_2024 = 3  # cycle index of the 2024-09-30 review


def edit(action, target, payload):
    return Edit(action=action, target=target, payload=payload, rationale="t", source="c")


def trained_harness():
    h = seed_harness("t")
    h = apply_edit(h, edit("set_focus_metric", "margin", {"definition": "(price - cost) x quantity"}))
    return apply_edit(h, edit("set_preference", "time_grain", {"value": "week"}))


@pytest.fixture
def db():
    from common.db import get_client

    return get_client()["co_retail"]


def report(findings, objective_values=()):
    objectives = [
        {"objective_id": "o1", "value": v, "target": 46, "status": "on_track", "note": "", "pipeline": {}}
        for v in objective_values
    ]
    return {
        "as_of": "2024-09-30",
        "harness_version": 3,
        "report": {"summary": "", "findings": findings, "objectives": objectives},
        "template_runs": [],
    }


DENVER = finding(id="denver", title="Denver margin spiked in August", metric="margin", direction="anomaly",
                 segment={"storeLocation": "Denver"}, period={"start": "2024-08-01", "end": "2024-08-31"})  # fmt: skip


@needs_atlas
async def test_perfect_report_scores_one(db):
    truth = await reference_value(db, CYCLES[0]["comments"][2]["check"]["reference"], date(2024, 9, 30))
    card = await score_report(db, "co_retail", report([DENVER], [truth]), trained_harness(), Q3_2024, TIME_FIELDS)
    assert card.recall.value == 1 and [c.id for c in card.recall.checks] == ["denver_test_accounts"]
    assert card.compliance.value == 1, card.compliance.checks
    assert card.objectives.value == 1 and card.reproducibility.value == 1
    assert card.total == 1 and card.other_findings == 0


@needs_atlas
async def test_failures_are_scored(db):
    wrong = {**DENVER, "metric": "revenue", "title": "Denver revenue spiked in August",
             "pipeline": {"collection": "sales", "stages": [{"$bogus": 1}]}}  # fmt: skip
    london = finding(id="london", title="London coupon use spiked", metric="coupon rate", direction="up",
                     segment={"storeLocation": "London"},
                     period={"start": "2023-11-01", "end": "2023-12-31"})  # fmt: skip
    card = await score_report(db, "co_retail", report([wrong, london], [12.3]), seed_harness("t"), Q3_2024,
                              TIME_FIELDS)  # fmt: skip
    failed = {c.id for c in card.compliance.checks if not c.passed}
    assert {"c01_margin_focus", "c02_skip_london_promo", "c03_weekly"} <= failed
    assert card.objectives.value == 0
    assert card.reproducibility.value == 0.5
    assert card.other_findings == 1


@needs_atlas
async def test_exclusion_resolves_test_account_story(db):
    h = apply_edit(trained_harness(), edit("add_exclusion", "qa", {
        "filter": {"customer.email": {"$regex": "@test\\.example\\.com$"}}, "reason": "QA"}))  # fmt: skip
    card = await score_report(db, "co_retail", report([]), h, Q3_2024, TIME_FIELDS)
    assert card.recall.checks[0].passed and card.recall.checks[0].detail == "resolved by exclusion"


def test_proof_page_renders_from_scorecards():
    from scripts.proof_chart import page

    def card(mode, cycle, as_of, total):
        part = {"value": total, "checks": []}
        return {"mode": mode, "cycle": cycle, "as_of": as_of, "total": total, "harness_version": cycle + 1,
                "recall": part, "compliance": part, "objectives": {"value": None}, "reproducibility": part}  # fmt: skip

    cards = [card("baseline", 0, "2023-12-31", 0.5), card("feedback", 0, "2023-12-31", 0.5),
             card("baseline", 1, "2024-03-31", 0.4), card("feedback", 1, "2024-03-31", 0.8)]  # fmt: skip
    cycles = [{"as_of": "2023-12-31", "kept": True,
               "reflection": {"edits": [{"action": "set_preference", "target": "time_grain",
                                         "payload": {"value": "week"}}], "diff": []}}]  # fmt: skip
    html = page("co_x", "r9", {"cards": cards, "cycles": cycles, "costs": {"feedback": 1.0, "baseline": 0.5}})
    assert "Q1 2024" in html and "Changed setting: time grain to week" in html and "1 kept" in html
    assert '"score": 80' in html and "$1.50" in html
