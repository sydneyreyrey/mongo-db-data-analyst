"""Tool definitions the analyst sees, and their dispatch to `CompanyData`."""

import json
from typing import Any

from analyst.data import MAX_ROWS_TO_MODEL, CompanyData
from analyst.report import Report

TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_collections",
        "description": "List the collections in the company database with document counts.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "describe_collection",
        "description": "Infer a collection's fields, types, example values, and date ranges from a sample.",
        "input_schema": {
            "type": "object",
            "properties": {"collection": {"type": "string"}},
            "required": ["collection"],
            "additionalProperties": False,
        },
    },
    {
        "name": "sample_docs",
        "description": "Return up to 5 random documents from a collection.",
        "input_schema": {
            "type": "object",
            "properties": {
                "collection": {"type": "string"},
                "n": {"type": "integer", "minimum": 1, "maximum": 5},
            },
            "required": ["collection"],
            "additionalProperties": False,
        },
    },
    {
        "name": "distinct_values",
        "description": "Most common values of a field with counts. Use name[] paths for array fields, "
        "e.g. items[].name.",
        "input_schema": {
            "type": "object",
            "properties": {"collection": {"type": "string"}, "field": {"type": "string"}},
            "required": ["collection", "field"],
            "additionalProperties": False,
        },
    },
    {
        "name": "run_aggregate",
        "description": "Run a read only MongoDB aggregation pipeline and return the result rows. "
        "Exclusions from the playbook are applied automatically before your stages. "
        'Write dates as {"$date": "2024-01-01T00:00:00Z"}. Convert decimals with $toDouble. '
        f"At most {MAX_ROWS_TO_MODEL} rows are shown, so aggregate before returning.",
        "input_schema": {
            "type": "object",
            "properties": {
                "collection": {"type": "string"},
                "stages": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["collection", "stages"],
            "additionalProperties": False,
        },
    },
    {
        "name": "submit_report",
        "description": "Submit the final report. Call this once, when your analysis is done. Each "
        "finding's pipeline is re-run to check it and its output rows feed the chart, so the chart "
        "x and y fields must exist in those rows.",
        "input_schema": Report.model_json_schema(),
    },
]


async def run_tool(data: CompanyData, name: str, args: dict[str, Any]) -> str:
    """Run a data tool and return the JSON text the model sees."""
    match name:
        case "list_collections":
            result: Any = await data.list_collections()
        case "describe_collection":
            result = await data.describe_collection(args["collection"])
        case "sample_docs":
            result = await data.sample_docs(args["collection"], args.get("n", 3))
        case "distinct_values":
            result = await data.distinct_values(args["collection"], args["field"])
        case "run_aggregate":
            rows = await data.aggregate(args["collection"], args["stages"])
            result = {"rows": rows[:MAX_ROWS_TO_MODEL], "row_count": len(rows)}
            if len(rows) > MAX_ROWS_TO_MODEL:
                result["note"] = f"Showing first {MAX_ROWS_TO_MODEL} of {len(rows)} rows."
        case _:
            raise ValueError(f"Unknown tool {name!r}")
    return json.dumps(result, default=str)
