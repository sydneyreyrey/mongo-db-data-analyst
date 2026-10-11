"""Phase 0 check: ping Atlas and make one dummy LLM call."""

import asyncio

from common import db, llm
from common.settings import get_settings


async def main() -> None:
    settings = get_settings()

    print("Atlas ping:", "ok" if await db.ping() else "FAILED")

    response, usage = await llm.call(
        "smoke",
        model=settings.analyst_model,
        max_tokens=256,
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": "Reply with the single word: ready"}],
    )
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    print(f"LLM reply: {text!r} (stop_reason={response.stop_reason})")
    print(
        f"Usage: model={usage.model} in={usage.input_tokens} out={usage.output_tokens} "
        f"cost=${usage.cost_usd:.6f} latency={usage.latency_s}s"
    )


if __name__ == "__main__":
    asyncio.run(main())
