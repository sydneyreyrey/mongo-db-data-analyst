import pytest

from common import db
from common.llm import BudgetExceeded, CostMeter, Usage, cost_usd
from common.settings import Settings, get_settings


def test_settings_defaults(monkeypatch):
    for var in ("ANALYST_MODEL", "MAX_ROUNDS"):
        monkeypatch.delenv(var, raising=False)
    s = Settings(_env_file=None)
    assert s.analyst_model == "claude-sonnet-5-5"
    assert s.max_rounds == 5


def test_cost_plain():
    # 1M in + 1M out on Sonnet 5.5 = $2 + $10
    assert cost_usd("claude-sonnet-5-5", 1_000_000, 1_000_000) == 12.0


def test_cost_with_cache():
    # Opus 5.5: 1M cache read = $0.20, 1M cache write = $4 * 1.25 = $5
    assert cost_usd("claude-opus-5-5", 0, 0, cache_read=1_000_000) == 0.2
    assert cost_usd("claude-opus-5-5", 0, 0, cache_write=1_000_000) == 5.0


def test_cost_unknown_model():
    with pytest.raises(ValueError):
        cost_usd("not-a-model", 1, 1)


def test_cost_meter_stops_at_budget():
    meter = CostMeter(limit_usd=1.0)
    usage = Usage(
        role="t",
        model="m",
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=0,
        cache_write_tokens=0,
        cost_usd=0.6,
        latency_s=0,
    )
    meter.add(usage)
    meter.check()
    meter.add(usage)
    with pytest.raises(BudgetExceeded):
        meter.check()


@pytest.mark.skipif(not get_settings().mongodb_uri, reason="MONGODB_URI not set")
async def test_atlas_ping():
    assert await db.ping()
