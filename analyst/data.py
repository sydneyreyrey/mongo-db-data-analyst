"""Read only access to one company database.

Everything the analyst sees goes through `CompanyData`. It is bound to a single database, only
reads, and applies the harness exclusions to every query in code (not by prompt).
"""

from datetime import datetime
from typing import Any

from bson import Decimal128, ObjectId, json_util
from pymongo.asynchronous.database import AsyncDatabase

from harness.schema import Harness

WRITE_STAGES = {"$out", "$merge"}
CROSS_COLLECTION_STAGES = {"$lookup", "$unionWith", "$graphLookup"}
FORBIDDEN_OPERATORS = {"$where", "$function", "$accumulator"}
MAX_ROWS_TO_MODEL = 100


class QueryRejected(ValueError):
    pass


def to_jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: to_jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [to_jsonable(v) for v in value]
    if isinstance(value, Decimal128):
        return float(value.to_decimal())
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, ObjectId):
        return str(value)
    return value


def parse_extended_json(value: Any) -> Any:
    """Turn model supplied JSON into BSON types, e.g. {"$date": "2024-01-01T00:00:00Z"}.

    Idempotent: values that are already BSON types (Regex, datetime) pass through unchanged.
    """
    return json_util.loads(json_util.dumps(value))


def _walk(value: Any):
    if isinstance(value, dict):
        for k, v in value.items():
            yield k, v
            yield from _walk(v)
    elif isinstance(value, list):
        for v in value:
            yield from _walk(v)


def check_pipeline(stages: list[dict], collections: set[str]) -> None:
    if not isinstance(stages, list) or not all(isinstance(s, dict) and len(s) == 1 for s in stages):
        raise QueryRejected("pipeline must be a list of single-key stage objects")
    for key, val in _walk(stages):
        if key in WRITE_STAGES:
            raise QueryRejected(f"{key} is not allowed: access is read only")
        if key in FORBIDDEN_OPERATORS:
            raise QueryRejected(f"{key} is not allowed")
        if key in CROSS_COLLECTION_STAGES:
            target = val if isinstance(val, str) else (val or {}).get("from") or (val or {}).get("coll")
            if not isinstance(target, str) or target not in collections:
                raise QueryRejected(f"{key} may only reference collections in this database")


class CompanyData:
    """One company's data, as the analyst is allowed to see it.

    `time_fields` maps a collection to its event date field. With `as_of` set, documents dated after
    it are invisible, including inside $lookup and $unionWith sub-pipelines.
    """

    def __init__(
        self,
        db: AsyncDatabase,
        harness: Harness,
        as_of: datetime | None = None,
        time_fields: dict[str, str] | None = None,
    ):
        self.db = db
        self.max_docs = harness.policy.constraints.max_docs
        self.max_time_ms = harness.policy.constraints.max_time_ms
        self.as_of = as_of
        self.time_fields = time_fields or {}
        filters = [parse_extended_json(e.filter) for e in harness.knowledge.exclusions]
        self.exclusion_match = {"$nor": filters} if filters else None

    def _visible(self, collection: str, query: dict | None = None) -> dict:
        """The filter every read of `collection` must pass: exclusions plus the as-of date."""
        parts = [q for q in (query, self.exclusion_match) if q]
        field = self.time_fields.get(collection)
        if self.as_of and field:
            parts.append({field: {"$lte": self.as_of}})
        return {"$and": parts} if len(parts) > 1 else (parts[0] if parts else {})

    def _guard(self, stages: list[dict]) -> list[dict]:
        """Push the visibility filter into every sub-pipeline that reads another collection."""
        out = []
        for stage in stages:
            ((name, spec),) = stage.items()
            if name == "$lookup" and isinstance(spec, dict):
                inner = [{"$match": self._visible(spec["from"])}, *self._guard(spec.get("pipeline", []))]
                stage = {name: {**spec, "pipeline": inner}}
            elif name == "$unionWith":
                spec = {"coll": spec} if isinstance(spec, str) else spec
                inner = [{"$match": self._visible(spec["coll"])}, *self._guard(spec.get("pipeline", []))]
                stage = {name: {**spec, "pipeline": inner}}
            elif name == "$graphLookup":
                restrict = self._visible(spec["from"], spec.get("restrictSearchWithMatch"))
                stage = {name: {**spec, "restrictSearchWithMatch": restrict}}
            elif name == "$facet":
                stage = {name: {k: self._guard(v) for k, v in spec.items()}}
            out.append(stage)
        return out

    async def collections(self) -> set[str]:
        names = await self.db.list_collection_names()
        return {n for n in names if not n.startswith("system.")}

    async def _require(self, collection: str) -> None:
        if collection not in await self.collections():
            raise QueryRejected(f"Unknown collection {collection!r}")

    async def list_collections(self) -> list[dict]:
        return [
            {"name": n, "documents": await self.db[n].count_documents(self._visible(n))}
            for n in sorted(await self.collections())
        ]

    async def describe_collection(self, collection: str, sample_size: int = 300) -> dict:
        """Field paths, types, and example values inferred from a sample, plus date ranges."""
        await self._require(collection)
        coll = self.db[collection]
        pipeline = [{"$match": self._visible(collection)}, {"$sample": {"size": sample_size}}]
        fields: dict[str, dict] = {}

        def visit(value: Any, path: str) -> None:
            if isinstance(value, dict):
                for k, v in value.items():
                    visit(v, f"{path}.{k}" if path else k)
                return
            if isinstance(value, list):
                if value and all(isinstance(v, dict) for v in value):
                    for v in value:
                        visit(v, f"{path}[]")
                    return
            info = fields.setdefault(path, {"types": set(), "examples": []})
            kind = {Decimal128: "decimal", datetime: "date", ObjectId: "objectId"}.get(
                type(value), type(value).__name__
            )
            info["types"].add(kind)
            example = to_jsonable(value)
            if len(info["examples"]) < 3 and example not in info["examples"]:
                info["examples"].append(example)

        async for doc in await coll.aggregate(pipeline):
            visit(doc, "")

        out = []
        for path, info in sorted(fields.items()):
            entry = {"field": path, "types": sorted(info["types"]), "examples": info["examples"]}
            if "date" in info["types"] and "[]" not in path:
                rng = await (
                    await coll.aggregate(
                        [
                            {"$match": self._visible(collection)},
                            {
                                "$group": {
                                    "_id": None,
                                    "min": {"$min": f"${path}"},
                                    "max": {"$max": f"${path}"},
                                }
                            },
                        ]
                    )
                ).to_list()
                if rng:
                    entry["range"] = [to_jsonable(rng[0]["min"]), to_jsonable(rng[0]["max"])]
            out.append(entry)
        return {
            "collection": collection,
            "documents": await coll.count_documents(self._visible(collection)),
            "fields": out,
            "notes": "Array fields are shown as name[]. Decimal values need $toDouble before arithmetic.",
        }

    async def sample_docs(self, collection: str, n: int = 3) -> list[dict]:
        await self._require(collection)
        n = max(1, min(n, 5))
        cursor = await self.db[collection].aggregate([{"$match": self._visible(collection)}, {"$sample": {"size": n}}])
        return to_jsonable(await cursor.to_list())

    async def distinct_values(self, collection: str, field: str, limit: int = 50) -> dict:
        await self._require(collection)
        pipeline = [
            {"$match": self._visible(collection)},
            {"$unwind": {"path": f"${field.split('[]')[0]}", "preserveNullAndEmptyArrays": True}}
            if "[]" in field
            else {"$match": {}},
            {"$sortByCount": f"${field.replace('[]', '')}"},
            {"$limit": limit},
        ]
        rows = await (await self.db[collection].aggregate(pipeline, maxTimeMS=self.max_time_ms)).to_list()
        return {
            "field": field,
            "values": [{"value": to_jsonable(r["_id"]), "count": r["count"]} for r in rows],
        }

    async def aggregate(self, collection: str, stages: list[dict]) -> list[dict]:
        """Run a read only pipeline with visibility enforced at every level and the row cap appended."""
        await self._require(collection)
        check_pipeline(stages, await self.collections())
        guarded = self._guard(parse_extended_json(stages))
        pipeline = [{"$match": self._visible(collection)}, *guarded, {"$limit": self.max_docs}]
        cursor = await self.db[collection].aggregate(pipeline, maxTimeMS=self.max_time_ms)
        return to_jsonable(await cursor.to_list())
