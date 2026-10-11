# Build Plan

Work phase by phase. Each phase ends with its checks passing. `CLAUDE.md` invariants override anything here.

## Phase 0: Scaffold (done, pending API key)

* `uv` project, settings, MongoDB connection, LLM wrapper with cost tracking, Makefile, tests

**Done when:** `make test` passes and `make smoke` pings Atlas and makes one LLM call with its cost reported.

## Phase 1: Company datasets and answer keys

* Load MongoDB sample datasets into the Atlas cluster (`sample_supplies`, `sample_analytics`)
* `scripts/build_company.py`: copy a sample dataset into a company database (`co_retail`, `co_bank`) and plant 5 to 6 known stories per company (a regional revenue drop in one month, a refund spike on one product, test accounts inflating totals, a segment growing fast, and so on). Deterministic seed.
* Write the answer key to `bench/<company>/planted.json`
* Write a scripted comment sequence to `bench/<company>/comments.json`: each comment has text the user would say plus a machine check (e.g. `{"type": "exclusion", "field": "customer.email", "pattern": "@test"}`)

**Done when:** both company databases exist in Atlas, rebuilding them gives identical data, and a test proves no module under `analyst/` or `api/` can load `bench/`.

## Phase 2: Harness store

* Pydantic `Harness` model per `CLAUDE.md`, stored per version in `harness_versions`
* Edit applier with typed edits and locked fields, plus a readable diff between versions
* Seed harness v1: generic business analyst, empty knowledge, default preferences
* Prompt builder rendering the harness in slot order, static part cached

**Done when:** every edit action produces a new version with a correct diff and locked edits raise.

## Phase 3: Analyze mode

* Read only data adapter and the five tools
* Orient pass: describe collections and write a short data map into the report context
* Agent loop producing a `Report` with findings, pipelines, and Vega-Lite charts, stored in `reports` with cost

**Done when:** `make report COMPANY=co_retail` writes a full report to MongoDB.

## Phase 4: Scorekeeper and baseline

* Plain code scoring: finding recall, precision, comment compliance, reproducibility
* `make baseline` scores harness v1 on both companies and prints a table

**Done when:** baseline numbers exist and scorer unit tests cover matching, partial overlap, and failed pipelines.

## Phase 5: Reflect mode and rounds

* Reflect: given the report and new comments, output typed edits (one or more, each tied to a comment)
* Round loop: report, comments, reflect, apply edits, new report, score, keep or revert
* `make rounds COMPANY=x N=5` runs the scripted user end to end, unattended

**Done when:** across rounds, recall and compliance beat baseline on both companies and the edit history is in MongoDB.

## Phase 6: Demo UI

* FastAPI routes: list companies, get latest report, post a comment, run a round, get harness diff and score history
* One HTML page: report with charts, comment box, harness diff timeline, score over rounds
* Live mode: a real person types a comment and watches the next report change

**Done when:** the full loop works from the browser.

## Stretch

* Atlas Vector Search over past comments and edits so Reflect recalls similar past feedback
* Third company dataset to show divergence
