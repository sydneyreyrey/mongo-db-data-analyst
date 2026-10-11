"""First contact with a new company database: work out which field dates each collection.

The as-of date and every time based analysis rely on this. A person can override the guess.
"""

from datetime import datetime
from typing import Any

from pymongo.asynchronous.database import AsyncDatabase

SAMPLE_SIZE = 300
# Dates that describe a person or thing rather than when an event happened.
NOT_EVENT_HINTS = ("birth", "dob", "expir", "valid_until")
EVENT_HINTS = ("date", "time", "created", "timestamp", "at", "day")


def _date_paths(doc: dict[str, Any], prefix: str = "") -> list[str]:
    """Dotted paths of datetime values, not descending into arrays."""
    out = []
    for key, value in doc.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, datetime):
            out.append(path)
        elif isinstance(value, dict):
            out.extend(_date_paths(value, path))
    return out


def _rank(path: str, presence: float) -> tuple:
    name = path.lower()
    not_event = any(h in name for h in NOT_EVENT_HINTS)
    event = any(h in name.rsplit(".", 1)[-1] for h in EVENT_HINTS)
    return (not not_event, presence >= 0.9, event, presence, -path.count("."))


async def detect_time_fields(db: AsyncDatabase) -> dict[str, str]:
    """For each collection, the most likely event date field. Collections without dates are omitted."""
    result = {}
    for name in sorted(await db.list_collection_names()):
        if name.startswith("system."):
            continue
        counts: dict[str, int] = {}
        n = 0
        async for doc in await db[name].aggregate([{"$sample": {"size": SAMPLE_SIZE}}]):
            n += 1
            for path in _date_paths(doc):
                counts[path] = counts.get(path, 0) + 1
        if not counts:
            continue
        best = max(counts, key=lambda p: _rank(p, counts[p] / n))
        if not any(h in best.lower() for h in NOT_EVENT_HINTS):
            result[name] = best
    return result
