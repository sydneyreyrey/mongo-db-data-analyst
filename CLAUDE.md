# Recursive Analyst

Connect it to a company's MongoDB with read access. It starts blank, orients itself, runs its own analysis, and presents what matters: findings with charts. A manager comments and sets goals. The analyst rewrites its own harness (rules, vocabulary, focus metrics, exclusions, objectives, saved templates, tools) so every future report reflects that feedback. We simulate a company's life as quarterly business reviews, and prove it generalizes across three very different businesses.

Built for the "Recursive Harnessing" challenge. MongoDB Atlas holds both the company data being analyzed and the agent's memory. Everything runs in the Atlas Hackathon Sandbox cluster.

## The system in one paragraph

One agent, two modes. Each **cycle** is one quarterly review with an `as_of` date; the analyst cannot see data after it. In **Analyze** mode the Analyst explores the company data through read only tools and submits a structured **Report**: findings (metric, segment, period, size, the pipeline that proves it, a chart) and the status of every active **Objective**. The manager (scripted for scoring, or a real person in the UI) leaves **Comments**: corrections, preferences, new goals, and reactions to findings ("keep this", "stop showing this"). In **Reflect** mode the same agent turns comments and off track objectives into typed **Harness** edits. Plain code applies them as a new harness version, which every later report uses. The **Scorekeeper** (plain code) scores each report; a round of edits that makes the next score drop is reverted.

## Memory

Modeled on human memory, all in MongoDB:

* **Working memory** (`working_memory`): lessons learned in chat. Applied at once in every conversation for that company, never read by reports.
* **Dreaming** (`dreams`): consolidation. Reviews working lessons, pending comments, off-track objectives, and recent reports; keeps, merges, or drops each lesson with a reason; writes a journal; saves one new harness version (`created_by: dream`). A button in the demo; a nightly schedule in production.
* **Long-term memory** (`harness_versions`): the harness. Reports read only this.
* **Episodic memory** (`reports`, `conversations`, `messages`).

## Companies (workspaces)

| Workspace | Business | Built from |
|---|---|---|
| `co_retail` | Office supply stores | `sample_supplies` |
| `co_rentals` | Home rental marketplace | `sample_airbnb` |
| `co_streaming` | Movie streaming service | `sample_mflix` |

Each has planted stories spread over time, a scripted manager timeline with comments and objectives, and its own harness. The analyst code contains nothing specific to any business.

## Non negotiable invariants

1. **The Scorekeeper is plain code.** No LLM calls anywhere in `scorekeeper/`.
2. **The agent never sees the answer key.** Planted stories, comment checks, and reference pipelines live on disk in `bench/`, never in MongoDB, and no module under `analyst/` or `api/` may load them. A test enforces this.
3. **Companies are sealed from each other.** Each workspace has its own harness lineage. Nothing learned for one company is ever read when working on another. No shared or global learned notebook. A test enforces this.
4. **The Harness is data, not code.** It only changes through typed edits in Reflect mode, each tied to a source (a comment id or an objective id). Never free form prompt edits, never locked fields.
5. **Company data access is read only, scoped, and time bounded.** The data adapter is bound to one company database and one `as_of` date. It only reads, rejects `$out`/`$merge`/code operators and cross database access, applies harness exclusions to every query, and hides documents after `as_of`. The agent must never see `sample_*` source data. Enforced in code, not by prompt.
6. **Every claim is reproducible.** Each finding, objective status, and template carries the pipeline that produced its numbers. The Scorekeeper re-runs them.
7. **The analyst's base instructions are business neutral.** A test checks they contain no business specific words.
8. **New outside connections need a human.** The analyst may enable tools from the vetted tool shelf on its own, but any new connector or MCP server is only ever a request a human approves.

## Stack

* Python 3.12, managed with `uv`
* MongoDB Atlas (sandbox `Cluster0`, via `MONGODB_URI`), `pymongo` async API
  * Company databases: `co_retail`, `co_rentals`, `co_streaming`
  * Memory database `MONGODB_DB` (default `recursive_analyst`): `workspaces`, `harness_versions`, `reports`, `comments`, `scorecards`, `cycles`
* Anthropic Python SDK. One model, `ANALYST_MODEL` (default `claude-sonnet-5-5`). Prompt caching on the harness portion.
* FastAPI plus one static HTML page with Vega-Lite charts for the demo UI
* No observability stack. Token and cost counts live on report records.
* pytest

## Repo layout

```
common/         settings, LLM client wrapper, MongoDB connection
analyst/        agent loop, data adapter, tools, tool shelf, prompt builder, analyze and reflect modes
harness/        harness schema, versioned store, edit applier, diff, locked fields
scorekeeper/    scoring (plain code only)
bench/          per company answer keys: planted stories, manager timeline with checks and reference pipelines
scripts/        build company datasets, run reports and cycles, smoke
api/            FastAPI app and static demo page
tests/
```

## Harness

Declaration order is prompt order. See `harness/schema.py` for the source of truth.

* `identity`: role, audience, values (values locked)
* `knowledge`: collections notes, vocabulary, rules, focus metrics, exclusions, objectives (metric, target, deadline), templates (named saved analyses with pipeline and chart, status `on` or `off`)
* `capabilities`: tools enabled from the shelf
* `preferences`: time grain, max findings, chart style
* `policy`: scope and limits (locked)
* `output`: report form

Every learned entry records its `source`.

## Edit actions

`annotate_collection`, `set_vocab`, `remove_vocab`, `add_rule`, `retire_rule`, `set_focus_metric`, `remove_focus_metric`, `add_exclusion`, `remove_exclusion`, `set_preference`, `set_objective`, `retire_objective`, `save_template`, `set_template_status`, `enable_tool`, `disable_tool`.

Each edit carries `action`, `target`, `payload`, `rationale`, and `source`.

## Analyst tools

* Core (always on, read only): `list_collections`, `describe_collection`, `sample_docs`, `distinct_values`, `run_aggregate`, `run_template`
* Tool shelf (enabled per company by Reflect, kept only if scores hold): `forecast`, `detect_spikes`, `compare_same_period_last_year`, `holiday_calendar`, `web_search` (cited)
* Connector requests: the analyst can write a request for a new data source; it is stored for a human to approve and never acted on automatically

## Templates

When the manager likes a finding, Reflect saves it as a template (its pipeline and chart, plus when to use it). Reports run every `on` template. When the manager says to stop, the template is set to `off`, not deleted, so it can be turned back on.

## Scoring (per cycle)

* **Story recall:** share of planted stories visible by `as_of` that a report finding matches (same metric family, overlapping segment and period, same direction)
* **Precision:** share of findings that match a planted story or are verified true by re-running their pipeline
* **Comment compliance:** share of earlier comment checks that pass (exclusions applied, metric used, grain, suppressed topics absent, templates on or off as requested)
* **Objective accuracy:** each active objective has a status whose value matches the reference pipeline within tolerance
* **Reproducibility:** every pipeline re-runs and matches its stated numbers

Keep or revert: a round of edits is kept only if the next report's total score does not drop. Stop after the timeline ends or `MAX_USD`.

## Conventions

* Small, typed modules. Pydantic models for every record and the harness.
* Every LLM call goes through `common/llm.py`, which returns model, tokens, cost, and latency so callers can store them and enforce `MAX_USD`.
* Secrets live in `.env` (gitignored). The repo is public; never commit credentials.
* Writing in docs, logs, and demo output: no em dashes.
* Explain progress to the user in plain, non technical language.
* Work in the phases in `BUILD_PLAN.md`. Finish a phase's checks before starting the next. Do not over engineer: one agent, no extra services.
