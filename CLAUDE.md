# Recursive Analyst

Connect it to a company's MongoDB with read access. It orients itself, runs its own analysis, and presents what matters: findings with charts. The user comments on the report, and the analyst rewrites its own harness (rules, vocabulary, focus metrics, exclusions, preferences) so the next report already reflects that feedback. Over a few rounds it becomes that company's analyst.

Built for the "Recursive Harnessing" challenge. MongoDB Atlas is both the company data being analyzed and the agent's memory (harness versions, reports, comments, edits). Everything runs in the Atlas Hackathon Sandbox cluster.

## The system in one paragraph

One agent, two modes. In **Analyze** mode the Analyst explores the company database through read only tools and produces a structured **Report** (findings, each with a metric, segment, period, the pipeline that proves it, and a chart). The user leaves **Comments**. In **Reflect** mode the same agent turns each comment into a typed **Harness** edit. Plain code applies the edits as a new harness version, the Analyst writes the next report, and the **Scorekeeper** (plain code) scores it. If the score dropped, the round is reverted.

## Non negotiable invariants

1. **The Scorekeeper is plain code.** No LLM calls anywhere in `scorekeeper/`.
2. **The agent never sees the answer key.** Planted findings and comment checks live on disk in `bench/`, never in MongoDB, and no module under `analyst/` or `api/` may load them. A test enforces this.
3. **The Harness is data, not code.** The harness only changes through typed edits in Reflect mode, each tied to a user comment. Never free form prompt edits, never locked fields.
4. **Company data access is read only and scoped.** The data adapter is bound to one company database (e.g. `co_retail`). It only allows `find`, `aggregate`, `count`, `distinct`, and rejects pipelines containing `$out`, `$merge`, or `$lookup`/`$unionWith` into another database. The agent must never see `sample_*` source data, which would reveal the planted changes. Enforced in code, not by prompt.
5. **Every claim is reproducible.** Each finding carries the aggregation pipeline that produced its numbers. The Scorekeeper re-runs it.

## Stack

* Python 3.12, managed with `uv`
* MongoDB Atlas (sandbox `Cluster0`, via `MONGODB_URI`), `pymongo` async API
  * Company databases: one per demo company (e.g. `co_retail`, `co_bank`), built from MongoDB sample datasets with planted findings
  * Memory database `MONGODB_DB` (default `recursive_analyst`): `workspaces`, `harness_versions`, `reports`, `comments`, `edits`, `scorecards`
* Anthropic Python SDK. One model, `ANALYST_MODEL` (default `claude-sonnet-5-5`). Prompt caching on the static harness portion.
* FastAPI plus one static HTML page with Vega-Lite charts for the demo UI
* No observability stack. Token and cost counts live on report records.
* pytest

## Repo layout

```
common/         settings, LLM client wrapper, MongoDB connection
analyst/        agent loop, tools, prompt builder, analyze and reflect modes
harness/        harness schema, versioned store, edit applier, diff, locked fields
scorekeeper/    planted finding and comment compliance scoring (plain code only)
bench/          per company answer keys: planted findings, scripted comments with checks
scripts/        build company datasets, run rounds, smoke
api/            FastAPI app and static demo page
tests/
```

## Harness schema

Declaration order is prompt order.

```json
{
  "workspace_id": "string",
  "version": 1,
  "parent_version": null,
  "identity":   { "role": "", "audience": "", "values": [] },
  "knowledge": {
    "collections":   { "<collection>": "note" },
    "vocabulary":    { "<term>": { "definition": "", "source_comment": "" } },
    "rules":         [ { "id": "", "text": "", "source_comment": "" } ],
    "focus_metrics": [ { "name": "", "definition": "", "source_comment": "" } ],
    "exclusions":    [ { "id": "", "filter": {}, "reason": "", "source_comment": "" } ]
  },
  "preferences": { "time_grain": "month", "max_findings": 5, "chart_style": "" },
  "policy":      { "scope": "", "constraints": { "max_docs": 1000, "max_time_ms": 10000 } },
  "output":      { "form": "" }
}
```

**Locked fields** (Reflect cannot edit): `identity.values`, `policy.scope`, read only access, `policy.constraints.max_time_ms` upper bound. Defined in `harness/locked.py`, enforced by the edit applier.

## Edit actions

Reflect expresses every change as one of: `annotate_collection`, `set_vocab`, `remove_vocab`, `add_rule`, `retire_rule`, `set_focus_metric`, `remove_focus_metric`, `add_exclusion`, `remove_exclusion`, `set_preference`.

Each edit carries `action`, `target`, `payload`, `rationale`, and `comment_id`.

## Analyst tools (read only)

`list_collections`, `describe_collection` (inferred schema from a sample), `sample_docs`, `distinct_values`, `run_aggregate` (capped by `max_docs` and `max_time_ms`).

## Report format

```json
{
  "summary": "",
  "findings": [ {
    "id": "", "title": "", "why_it_matters": "",
    "metric": "", "segment": { "<field>": "<value>" },
    "period": { "start": "", "end": "" }, "direction": "up|down|anomaly",
    "magnitude": 0.0,
    "pipeline": { "collection": "", "stages": [] },
    "chart": { "vega_lite": {} }
  } ],
  "applied": { "exclusions": [], "focus_metrics": [], "time_grain": "" }
}
```

## Scoring

* **Finding recall:** share of planted findings matched by a report finding (same metric family, overlapping segment and period, same direction).
* **Precision:** share of report findings that match a planted finding or are verified true by re-running their pipeline.
* **Comment compliance:** share of earlier comments whose code check passes on the new report (e.g. test accounts excluded, margin used, weekly grain).
* **Reproducibility:** every finding's pipeline re-runs and matches its stated magnitude.

Keep or revert: a round is kept only if the total score does not drop. Stop after `MAX_ROUNDS` or `MAX_USD`.

## Conventions

* Small, typed modules. Pydantic models for every record and the harness.
* Every LLM call goes through `common/llm.py`, which returns model, tokens, cost, and latency so callers can store them and enforce `MAX_USD`.
* Secrets live in `.env` (gitignored). The repo is public; never commit credentials.
* Writing in docs, logs, and demo output: no em dashes.
* Work in the phases in `BUILD_PLAN.md`. Finish a phase's checks before starting the next. Do not over engineer: one agent, no extra services.
