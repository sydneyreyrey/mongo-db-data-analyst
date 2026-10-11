# Build Plan

Work phase by phase. Each phase ends with its checks passing. `CLAUDE.md` invariants override anything here.

## Phase 0: Scaffold (done)

`uv` project, settings, MongoDB connection, LLM wrapper with cost tracking, Makefile, tests. `make smoke` passes.

## Phase 1: Retail dataset (done)

`co_retail` built from `sample_supplies` with six planted stories, deterministic rebuild, answer key in `bench/co_retail/planted.json`.

## Phase 2: Harness store (done)

Harness model, typed edits with locked fields, versioned storage, diffs, reverts, prompt builder.

## Phase 3: Analyze mode (done)

Read only scoped data adapter with exclusions, core tools, report with pipelines and Vega-Lite charts built from real results, `make report COMPANY=co_retail`. First report found about 2 of 6 stories.

## Phase 4: Time, goals, and templates

* Workspace records each collection's time field; the data adapter hides documents after `as_of`
* Harness gains `objectives`, `templates` (on or off), and `capabilities`; new edit actions; every entry records `source`
* Report gains objective status (value, target, on or off track, pipeline) and which templates it ran
* `run_template` core tool
* `bench/co_retail/timeline.json`: nine quarterly reviews (Q4 2023 to Q4 2025), each with manager comments, objectives, and template reactions, every one with a machine check; objectives carry a reference pipeline for scoring
* Test: analyst base instructions contain no business specific words

**Done when:** a report can be produced `as_of` any quarter end, and objective and template edits round trip through the store.

## Phase 5: Scorekeeper and baseline

* Plain code scoring per `CLAUDE.md`: story recall, precision, compliance, objective accuracy, reproducibility
* `make baseline COMPANY=co_retail`: run all nine quarters with the blank harness and no feedback, store scorecards

**Done when:** baseline scores exist per quarter and scorer unit tests cover matching, overlap, failed pipelines, and objective tolerance.

## Phase 6: Reflect and the cycle loop

* Reflect mode: given the report, the new comments, and objective status, output typed edits, each tied to a comment or objective
* Cycle loop: report, comments, reflect, apply edits, next quarter's report, score, keep or revert
* `make cycles COMPANY=co_retail` runs the full timeline unattended and stores everything in MongoDB

**Done when:** across the timeline, scores beat the baseline and the notebook history explains why.

## Phase 7: Tool shelf

* `forecast`, `detect_spikes`, `compare_same_period_last_year`, `holiday_calendar`, `web_search` (cited)
* Reflect may enable or disable shelf tools with a reason; kept only if scores hold
* Connector requests stored for human approval

**Done when:** at least one tool is enabled by Reflect during a run and kept on merit.

## Phase 8: Two more businesses

* `co_rentals` from `sample_airbnb` and `co_streaming` from `sample_mflix`, each with planted stories, a manager timeline, and objectives
* Test: no company's harness or reports reference another company

**Done when:** the same code runs all three timelines and the final harnesses clearly diverge.

## Phase 9: Demo app

* FastAPI routes: companies, reports, comments, run a cycle, harness diff and history, scores over time, connector requests
* One page per company: latest report with charts, comment box with keep and stop buttons per finding, notebook timeline, score over quarters, side by side view of the three companies' notebooks

**Done when:** the full loop works from the browser with a real person giving feedback.
