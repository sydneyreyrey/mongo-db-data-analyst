import pytest

from analyst.agent import _validate_report
from analyst.report import chart_problems, sentences, vega_lite
from common.settings import get_settings
from harness.schema import Chart
from harness.seed import seed_harness

GOOD = Chart(type="bar", x="_id", y="margin_usd", title="Denver earns the most profit", x_label="Store",
             y_label="Profit ($)", y_format="currency")  # fmt: skip


@pytest.mark.parametrize(
    ("chart", "problem"),
    [
        (GOOD.model_copy(update={"title": ""}), "title is required"),
        (GOOD.model_copy(update={"x_label": "_id"}), "looks like a field name"),
        (GOOD.model_copy(update={"y_label": "margin_usd"}), "looks like a field name"),
        (GOOD.model_copy(update={"color": "store", "color_label": ""}), "color_label is required"),
        (GOOD.model_copy(update={"title": "x" * 95}), "too long"),
    ],
)
def test_unreadable_charts_are_rejected(chart, problem):
    assert any(problem in p for p in chart_problems(chart))


def test_good_chart_has_no_problems_and_no_field_names_on_screen():
    rows = [{"_id": "Denver", "margin_usd": 1200.5}, {"_id": "Austin", "margin_usd": 300.0}]
    assert chart_problems(GOOD, rows) == []
    spec = vega_lite(GOOD, rows)
    enc = spec["encoding"]
    assert spec["title"]["text"] == "Denver earns the most profit"
    assert enc["x"]["title"] == "Store" and enc["y"]["title"] == "Profit ($)"
    assert enc["y"]["axis"]["format"] == "$,.0f" and enc["x"]["sort"] == "-y"
    assert all(t["title"] for t in enc["tooltip"])  # hover text uses plain labels too


def test_too_many_series():
    rows = [{"w": i, "v": 1, "s": f"s{i}"} for i in range(7)]
    chart = Chart(type="line", x="w", y="v", color="s", title="t", x_label="Week", y_label="Value", color_label="S")
    assert any("at most 5" in p for p in chart_problems(chart, rows))


def test_sentence_count():
    assert sentences("One. Two! Three?") == 3 and sentences("Margin fell 4.5% in Q3. Watch Denver.") == 2


needs_atlas = pytest.mark.skipif(not get_settings().mongodb_uri, reason="MONGODB_URI not set")


@needs_atlas
async def test_report_needs_concrete_title_and_short_focus():
    from analyst.data import CompanyData
    from common.db import get_client

    data = CompanyData(get_client()["co_retail"], seed_harness("t"))
    report = {"title": "General review", "summary": "s", "findings": [], "applied": {},
              "focus": {"headline": "", "detail": "One. Two. Three. Four."}}  # fmt: skip
    with pytest.raises(ValueError) as e:
        await _validate_report(data, seed_harness("t"), report)
    assert "title is generic" in str(e.value) and "focus headline is empty" in str(e.value)
    assert "4 sentences" in str(e.value)


def test_jargon_spotting():
    from analyst.style import jargon

    assert jargon("Denver margin falling since July peak, driven by laptops") == ["peak"]
    assert jargon("Revenue up 12% YoY vs. last year") == ["vs", "yoy"]
    assert jargon("Our Denver stores are making less profit each month since July") == []


def test_writing_style_is_simple_by_default_and_learnable():
    from analyst.prompt_builder import render
    from harness.edits import Edit, apply_edit

    h = seed_harness("t")
    text = render(h)[-1]["text"]
    assert "Writing style: simple" in text and "smart high school student" in text
    detailed = apply_edit(h, Edit(action="set_preference", target="writing_style", payload={"value": "technical"},
                                  rationale="reader asked for more detail", source="m1"))  # fmt: skip
    assert "Writing style: technical" in render(detailed)[-1]["text"]


@needs_atlas
async def test_simple_style_sends_back_shorthand_but_technical_allows_it():
    from analyst.data import CompanyData
    from common.db import get_client
    from harness.edits import Edit, apply_edit

    report = {"title": "Denver margin falling since July peak, driven by laptops", "summary": "s", "findings": [],
              "applied": {}, "focus": {"headline": "Profit went down.", "detail": "It fell."}}  # fmt: skip
    simple = seed_harness("t")
    with pytest.raises(ValueError, match="plain everyday words"):
        await _validate_report(CompanyData(get_client()["co_retail"], simple), simple, report)
    technical = apply_edit(simple, Edit(action="set_preference", target="writing_style",
                                        payload={"value": "technical"}, rationale="r", source="m"))  # fmt: skip
    ok, _ = await _validate_report(CompanyData(get_client()["co_retail"], technical), technical, report)
    assert ok.title.startswith("Denver margin")


def test_time_periods_stay_in_order_on_charts():
    months = [{"m": "2024-08", "s": 128}, {"m": "2024-07", "s": 64}, {"m": "2024-09", "s": 40}]
    spec = vega_lite(Chart(type="bar", x="m", y="s", title="t", x_label="Month", y_label="Sales ($)"), months)
    assert spec["encoding"]["x"]["type"] == "temporal" and "sort" not in spec["encoding"]["x"]
    assert spec["encoding"]["x"]["axis"]["format"] == "%b %Y" and spec["data"]["values"][0]["m"] == "2024-08-01"
    weeks = [{"w": "2024-W35", "s": 1}, {"w": "2024-W33", "s": 2}]
    spec = vega_lite(Chart(type="bar", x="w", y="s", title="t", x_label="Week", y_label="S"), weeks)
    assert spec["encoding"]["x"]["sort"] == "ascending"
    stores = [{"k": "Denver", "s": 1}, {"k": "Austin", "s": 2}]
    spec = vega_lite(Chart(type="bar", x="k", y="s", title="t", x_label="Store", y_label="S"), stores)
    assert spec["encoding"]["x"]["sort"] == "-y"  # only real categories are sorted biggest first


def test_custom_format_takes_over_the_instructions():
    from datetime import date

    from analyst.agent import kickoff
    from harness.seed import DEFAULT_FORM, has_custom_format

    assert not has_custom_format(DEFAULT_FORM)
    assert has_custom_format("Sales across all stores, then costs, then profit.")
    assert "overrides the default structure" in kickoff("co", date(2024, 9, 30), [], [], None, True)
    assert "overrides the default structure" not in kickoff("co", date(2024, 9, 30), [], [], None, False)


@needs_atlas
async def test_sections_without_charts_and_format_proof():
    from analyst.data import CompanyData
    from common.db import get_client
    from harness.edits import Edit, apply_edit

    total = {"collection": "sales", "stages": [{"$count": "orders"}]}
    section = {"id": "overview", "title": "Sales held steady across all stores.", "why_it_matters": "About the same.",
               "metric": "sales", "segment": {}, "period": {"start": "2024-09-01", "end": "2024-09-30"},
               "direction": "flat", "magnitude": 0.0, "pipeline": total}  # fmt: skip
    report = {"title": "Sales held steady in September", "summary": "s", "findings": [section], "applied": {},
              "focus": {"headline": "Sales held steady.", "detail": "Nothing big changed."}}  # fmt: skip
    plain = seed_harness("t")
    data = CompanyData(get_client()["co_retail"], plain)
    ok, charts = await _validate_report(data, plain, report)
    assert ok.findings[0].chart is None and charts == {}  # a section can be text only

    custom = apply_edit(plain, Edit(action="set_report_format", target="report", rationale="r", source="m",
                                    payload={"format": "Sales, then costs, then profit."}))  # fmt: skip
    with pytest.raises(ValueError, match="format_followed"):
        await _validate_report(data, custom, report)
    followed = {**report, "format_followed": ["Sales first: the overview section.", "Costs: ...", "Profit: ..."]}
    assert (await _validate_report(data, custom, followed))[0].format_followed


def test_single_value_charts_are_rejected():
    one = [{"m": "2024-08", "s": 75938.9}]
    chart = Chart(type="bar", x="m", y="s", title="Test orders in August", x_label="Month", y_label="Revenue ($)")
    assert any("only one value" in p for p in chart_problems(chart, one))
    assert not any("only one value" in p for p in chart_problems(chart, one + [{"m": "2024-07", "s": 1.0}]))
