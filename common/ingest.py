"""Load an uploaded CSV into a new company database, with column types inferred."""

import csv
import io
import re
from datetime import datetime
from typing import Any

from pymongo.asynchronous.mongo_client import AsyncMongoClient

MAX_BYTES = 25 * 1024 * 1024
MAX_ROWS = 200_000
DATE_FORMATS = ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y", "%m/%d/%Y %H:%M", "%d/%m/%Y", "%Y/%m/%d")
PROTECTED_PREFIXES = ("sample_", "admin", "local", "config", "recursive_analyst")


class IngestError(ValueError):
    pass


def slug(text: str, max_len: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return (s or "data")[:max_len]


def _parse_date(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        pass
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _parse_number(value: str) -> int | float | None:
    cleaned = value.replace(",", "").replace("$", "").strip()
    try:
        return int(cleaned)
    except ValueError:
        pass
    try:
        return float(cleaned)
    except ValueError:
        return None


def infer_types(rows: list[dict[str, str]]) -> dict[str, str]:
    """A column is a number or date only if every non-empty value parses as one."""
    types = {}
    for column in rows[0] if rows else {}:
        values = [r[column].strip() for r in rows if r.get(column) and r[column].strip()]
        if not values:
            types[column] = "string"
        elif all(v.lower() in ("true", "false") for v in values):
            types[column] = "bool"
        elif all(_parse_number(v) is not None for v in values):
            types[column] = "number"
        elif all(_parse_date(v) is not None for v in values):
            types[column] = "date"
        else:
            types[column] = "string"
    return types


def convert(row: dict[str, str], types: dict[str, str]) -> dict[str, Any]:
    doc = {}
    for column, raw in row.items():
        key = slug(column, 60) if column else "column"
        value = (raw or "").strip()
        if not value:
            doc[key] = None
        elif types[column] == "number":
            doc[key] = _parse_number(value)
        elif types[column] == "date":
            doc[key] = _parse_date(value)
        elif types[column] == "bool":
            doc[key] = value.lower() == "true"
        else:
            doc[key] = value
    return doc


async def load_csv(client: AsyncMongoClient, db_name: str, collection: str, data: bytes) -> dict[str, Any]:
    """Create `db_name.collection` from CSV bytes. Refuses to touch an existing or protected database."""
    if len(data) > MAX_BYTES:
        raise IngestError(f"CSV is larger than {MAX_BYTES // (1024 * 1024)} MB")
    if db_name.startswith(PROTECTED_PREFIXES) or not db_name.startswith("co_"):
        raise IngestError(f"Database name {db_name!r} is not allowed")
    if db_name in await client.list_database_names():
        raise IngestError(f"Database {db_name!r} already exists")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise IngestError("CSV must be UTF-8 encoded") from e
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        raise IngestError("CSV has no data rows")
    if len(rows) > MAX_ROWS:
        raise IngestError(f"CSV has more than {MAX_ROWS} rows")
    types = infer_types(rows)
    docs = [convert(r, types) for r in rows]
    await client[db_name][collection].insert_many(docs)
    return {
        "database": db_name,
        "collection": collection,
        "rows": len(docs),
        "columns": {slug(c, 60): t for c, t in types.items()},
    }
