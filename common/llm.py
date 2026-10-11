"""Single entry point for every LLM call.

Each call returns the response plus a `Usage` record (model, tokens, cost,
latency) so callers can store it on run and experiment records and enforce
MAX_USD through a shared `CostMeter`.
"""

import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import anthropic
from pydantic import BaseModel

from common.settings import get_settings

FALLBACK_BETA = "server-side-fallback-2026-07-01"

# USD per million tokens: (input, output, cache read). Cache writes (5 minute TTL) bill at 1.25x input.
PRICES: dict[str, tuple[float, float, float]] = {
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
}
CACHE_WRITE_MULTIPLIER = 1.25


class Usage(BaseModel):
    role: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_usd: float
    latency_s: float


def cost_usd(model: str, input_tokens: int, output_tokens: int, cache_read: int = 0, cache_write: int = 0) -> float:
    if model not in PRICES:
        raise ValueError(f"No price for model {model!r}; add it to common.llm.PRICES")
    inp, out, read = PRICES[model]
    total = input_tokens * inp + output_tokens * out + cache_read * read + cache_write * inp * CACHE_WRITE_MULTIPLIER
    return round(total / 1_000_000, 6)


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class CostMeter:
    """Running spend for one session (a baseline run or a coach session)."""

    limit_usd: float
    spent_usd: float = 0.0

    def add(self, usage: Usage) -> None:
        self.spent_usd += usage.cost_usd

    def check(self) -> None:
        if self.spent_usd >= self.limit_usd:
            raise BudgetExceeded(f"Spent ${self.spent_usd:.2f} of ${self.limit_usd:.2f}")


@lru_cache
def get_client() -> anthropic.AsyncAnthropic:
    key = get_settings().anthropic_api_key
    return anthropic.AsyncAnthropic(api_key=key) if key else anthropic.AsyncAnthropic()


async def call(role: str, meter: CostMeter | None = None, **params: Any) -> tuple[Any, Usage]:
    """Make one Messages API request.

    `role` labels the caller ("analyst", "coach", "smoke"). `params` are passed
    to `messages.create` unchanged. If a meter is given, its budget is checked
    before the call and the cost is added after.
    """
    if meter:
        meter.check()

    settings = get_settings()
    client = get_client()
    start = time.perf_counter()
    if settings.refusal_fallback:
        response = await client.beta.messages.create(
            betas=[FALLBACK_BETA], extra_body={"fallbacks": "default"}, **params
        )
    else:
        response = await client.messages.create(**params)
    latency = time.perf_counter() - start

    u = response.usage
    cache_read = u.cache_read_input_tokens or 0
    cache_write = u.cache_creation_input_tokens or 0
    # Price by the model that actually served the request (a fallback may differ from the one requested).
    usage = Usage(
        role=role,
        model=response.model,
        input_tokens=u.input_tokens,
        output_tokens=u.output_tokens,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        cost_usd=cost_usd(response.model, u.input_tokens, u.output_tokens, cache_read, cache_write),
        latency_s=round(latency, 3),
    )
    if meter:
        meter.add(usage)
    return response, usage
