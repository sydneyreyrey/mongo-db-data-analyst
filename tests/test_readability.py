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
