# Recursive Analyst API

Everything the web app needs: connect or upload a company, run reports, chat with the analyst while it learns, let it dream (turn what it learned into long-term memory), compare report versions, and show how scores moved.

## How the analyst's memory works

It works like a person's memory, and all of it lives in MongoDB:

| Memory | What it is | Collection |
|---|---|---|
| Exploration | Back and forth what-ifs with charts. Nothing is learned until the reader keeps a step. | `explorations` |
| Working memory | Lessons from chat instructions and kept explorations, each with a strength. Used right away in conversations, not yet permanent. | `working_memory` |
| Dreaming | Consolidation: takes the strong lessons (plus pending comments, off-track goals, recent reports); keeps, merges, or drops each with a reason; writes a journal. Weak lessons wait, and fade if never reinforced. | `dreams` |
| Long-term memory | The notebook (harness). Reports only read this, so new reports change after a dream. Every version is kept. | `harness_versions` |
| Episodic memory | What happened: reports and conversations. | `reports`, `conversations`, `messages` |

**What lands is decided by evidence, not by the AI alone.** Every lesson has a strength: a chat instruction
counts 2, pressing "remember this" on an exploration step counts 3, coming back to the same idea adds 1, and
"not useful" subtracts 2. Only lessons with strength 2 or more reach a dream. Weaker ones wait, and fade
after two dreams without reinforcement.

In production, dreaming would run on a schedule (for example nightly, with a cron job or an Atlas Scheduled
Trigger calling `POST /companies/{id}/dream`). In the demo it is a Dream button.

* Base URL (local): `http://localhost:8000`
* Interactive docs: `http://localhost:8000/docs` (try every call in the browser)
* Machine readable spec: `http://localhost:8000/openapi.json` (also checked in at `docs/openapi.json`, for client code generation)
* All requests and responses are JSON, except the CSV upload (multipart form).
* No auth. CORS is open by default; restrict it with `CORS_ORIGINS=https://your-app.example` in `.env`.

## Quick start

```bash
make api   # serves on http://localhost:8000
```

Needs `.env` with `MONGODB_URI` and `ANTHROPIC_API_KEY` (see `.env.example`).

## Costs: four endpoints spend money

Only these calls use the AI model. Everything else (memory, compare, undo, forget) is free.

| Call | What it does | Typical cost |
|---|---|---|
| `POST /companies/{id}/reports` | The analyst explores the data and writes a report | $0.06 to $0.15 |
| `POST /companies/{id}/chat` | One chat message: the analyst answers and may keep a lesson | $0.01 to $0.08 |
| `POST /companies/{id}/dream` | Turns working memory into long-term memory | $0.02 to $0.08 |
| `POST /companies/{id}/reflect` | Older batch version of dreaming, used by the simulator | $0.01 to $0.05 |

Consider a confirm step in the UI before these, and avoid calling them on page load.

## The core loop (what the demo screens do)

```
1. Pick or create a company     GET /companies   |  POST /companies/upload  |  POST /companies
2. Run a report                 POST /companies/{id}/reports  -> job, poll GET /jobs/{job_id}
3. Show the report              GET /reports/{report_id}  (or GET /companies/{id}/reports/latest)
4. Chat or explore              POST /companies/{id}/chat   mode "chat": it answers and keeps lessons
                                                            mode "explore": what-ifs with charts, nothing kept
   Keep what lands              POST /companies/{id}/explorations/{step_id}/keep    ("remember this")
                                POST /companies/{id}/explorations/{step_id}/reject  ("not useful")
5. Show working memory          the reply's `lessons`, and GET /companies/{id}/memory
                                (each lesson can be forgotten: DELETE /companies/{id}/memory/{lesson_id})
6. Dream                        POST /companies/{id}/dream  -> job; show the journal and what changed
7. Run the next report          back to step 2; it now uses what the dream made permanent
8. Compare report versions      GET /reports/compare?a={before dream}&b={after dream}
```

The older batch flow (post comments, then `reflect`) still works and is what the simulator uses. A dream also
picks up any pending comments, so comments posted without chatting are not lost.

## Slow calls are background jobs

`POST .../reports` and `POST .../reflect` return `202` immediately with a job. Poll it every few seconds until `status` is `done` or `failed`. A report usually takes 30 to 120 seconds; reflect 10 to 40 seconds. Only one job runs per company at a time (a second one gets `409`).

```json
// 202 response, and GET /jobs/{job_id} while running
{ "_id": "6acbdd0ae86f99a1a46b3cee", "kind": "report", "workspace_id": "co_retail_trial",
  "status": "running", "created_at": "2026-10-11T19:01:31.002000" }

// GET /jobs/{job_id} when finished
{ "_id": "...", "kind": "report", "status": "done", "finished_at": "...",
  "result": { "id": "6acbdd85adca18a8e7733000", "as_of": "2024-09-30", "harness_version": 2, "cost_usd": 0.0827,
              "findings": ["Denver margin falling since July peak, driven by laptops", "..."] } }

// or
{ "_id": "...", "status": "failed", "error": "AnalysisFailed: No valid report after 30 turns" }
```

```ts
async function waitForJob(jobId: string): Promise<any> {
  for (;;) {
    const job = await (await fetch(`${API}/jobs/${jobId}`)).json();
    if (job.status === "done") return job.result;
    if (job.status === "failed") throw new Error(job.error);
    await new Promise((r) => setTimeout(r, 3000));
  }
}
```

If the server restarts, jobs that were running are marked `failed` with `error: "server restarted"`.

---

## Companies

A company (also called a workspace) is one database plus its own notebook. Companies never share anything.

### `GET /companies`

```json
[
  { "id": "co_retail_trial", "_id": "co_retail_trial", "name": "Retail trial", "company_db": "co_retail",
    "time_fields": { "sales": "saleDate" }, "source": "existing", "created_at": "2026-10-11T19:01:30.441000",
    "harness_version": 2 }
]
```

`source` is `existing`, `csv`, or `simulation` (the simulator's own copies; you may want to hide those in a company picker). `time_fields` says which field dates each collection; it is detected automatically.

### `POST /companies/upload` (multipart form): bring your own CSV

Fields: `file` (the CSV, UTF-8, up to 25 MB / 200,000 rows) and optional `name`.

```bash
curl -F file=@orders.csv -F name="Acme Shop" http://localhost:8000/companies/upload
```

```json
// 201
{ "_id": "co_acme_shop", "name": "Acme Shop", "company_db": "co_acme_shop", "time_fields": { "orders": "order_date" },
  "source": "csv", "created_at": "...",
  "loaded": { "database": "co_acme_shop", "collection": "orders", "rows": 1200,
              "columns": { "order_id": "number", "order_date": "date", "region": "string", "amount": "number",
                           "returned": "bool" } } }
```

Column names are normalized to snake_case. Types are inferred: a column is a number, date, or true/false only if every value parses as one. If the name is taken, a suffix is added (`co_acme_shop_2`). `400` on a bad file.

### `POST /companies`: connect a database already in the cluster

```json
// body
{ "company_db": "co_retail", "workspace_id": "co_retail_demo", "name": "Retail demo" }
```

`workspace_id` defaults to `company_db`. Connecting the same database under several workspace ids gives independent notebooks over the same data, which is handy for a clean demo. Returns the company (`201`). `400` for protected databases (`sample_*`, system), `404` if the database does not exist.

### `GET /companies/{id}`

Same shape as one item of `GET /companies`.

### `PUT /companies/{id}/time-fields`: correct the detected date fields

```json
{ "time_fields": { "orders": "shipped_at" } }
```

---

## Reports

### `POST /companies/{id}/reports`: run a report (costs money, background job)

```json
// body (optional)
{ "as_of": "2024-09-30" }
```

`as_of` is the review date: the analyst cannot see any data after it. Omit it for "today" (all data). Returns a job; its result is a report summary (`id`, `as_of`, `harness_version`, `cost_usd`, `findings` titles).

### `GET /reports/{report_id}` and `GET /companies/{id}/reports/latest`

The full report.

```json
{
  "_id": "6acbdd85adca18a8e7733000",
  "workspace_id": "co_retail_trial",
  "harness_version": 2,
  "as_of": "2024-09-30",
  "created_at": "2026-10-11T19:03:33.415000",
  "cost_usd": 0.0827,
  "turns": 6,
  "report": {
    "summary": "Plain text summary of the review...",
    "findings": [
      {
        "id": "denver_margin_decline",
        "title": "Denver margin falling since July peak, driven by laptops",
        "why_it_matters": "Denver is the largest store...",
        "metric": "margin",
        "segment": { "storeLocation": "Denver" },
        "period": { "start": "2024-07-01", "end": "2024-09-30" },
        "direction": "down",
        "magnitude": -0.369,
        "dollar_impact": -10765.65,
        "pipeline": { "collection": "sales", "stages": [ "...the MongoDB query that proves it..." ] },
        "chart": { "type": "line", "x": "month", "y": "margin", "color": null, "title": "Denver monthly margin" }
      }
    ],
    "objectives": [
      { "objective_id": "o1_margin_2024", "value": 45.8, "target": 46, "status": "off_track",
        "note": "Low margin paper volume is pulling the rate down.", "pipeline": { "...": "..." } }
    ],
    "applied": { "focus_metrics": ["margin"], "time_grain": "week" }
  },
  "charts": { "denver_margin_decline": { "$schema": "https://vega.github.io/schema/vega-lite/v5.json", "...": "..." } },
  "template_runs": [ { "id": "weekly_margin_trend", "name": "Weekly gross margin trend", "ok": true, "error": "" } ],
  "template_charts": { "weekly_margin_trend": { "$schema": "...vega-lite..." } },
  "steps": [ { "turn": 1, "tool": "list_collections", "input": {}, "ok": true, "output_preview": "...", "seconds": 0.06 } ],
  "usage": [ "...per model call: tokens, cost, latency..." ]
}
```

How to render it:

* **Findings**: `title`, `why_it_matters`, and a meta line from `metric`, `direction`, `magnitude` (a fraction: `-0.369` is -37%), `period`, `dollar_impact`.
* **Charts**: `charts[finding.id]` is a complete Vega-Lite spec with its data inline. Render with [vega-embed](https://github.com/vega/vega-embed): `vegaEmbed(el, report.charts[f.id], { actions: false })`. The data is the finding's own query result, so the chart cannot disagree with the numbers.
* **Saved views**: `template_runs` lists the views the manager asked to see every time; render `template_charts[run.id]` for each `ok` run. Use a run's `id` as the `template_id` for a stop button.
* **Goals**: `report.objectives`; `status` is `on_track`, `off_track`, `met`, or `missed`.
* **How it worked** (optional): `steps` is every tool call the analyst made, and `turns` and `cost_usd` are totals.

`period`, `segment`, and `pipeline` contents vary by company; treat them as display data.

### `GET /companies/{id}/reports`

Newest first: `[{ "id", "as_of", "harness_version", "created_at", "cost_usd", "findings": ["title", ...] }]`.

---

## Chat and explore: talking with the analyst

One endpoint, two modes, chosen when a conversation starts (later messages in it keep its mode):

* **chat**: the analyst answers, and turns instructions for the future into lessons in working memory.
* **explore**: back and forth what-ifs. Each result comes back as a step with its own chart. Nothing is
  learned; the reader presses "remember this" on the steps that land.

### `POST /companies/{id}/chat` (costs money; waits for the reply)

```json
// body
{ "message": "Exclude test accounts, anything with an @test.example.com email.",
  "mode": "chat",                      // or "explore"; only used when starting a conversation
  "conversation_id": null,            // omit to start a new conversation; reuse the returned id to continue
  "report_id": "6acbdd85...",          // optional; defaults to the latest report (or the conversation's report)
  "finding_id": null,                  // optional: the chart the user is pointing at ("why is this down?")
  "template_id": null }                // optional: a saved view the user is pointing at
```

Usually returns in 5 to 40 seconds (longer if it has to look at the data). No job polling needed.

```json
{
  "conversation_id": "conv_6acc...",
  "mode": "chat",
  "message_id": "m_6acc...",           // the user's message
  "reply_id": "m_6acc...",             // the analyst's reply
  "reply": "Got it. From now on I will leave out @test.example.com accounts; they are 40 sales in August 2024.",
  "lessons": [
    { "_id": "lesson_6acc...", "status": "working", "strength": 2, "text": "Exclude test accounts (@test.example.com).",
      "edits": [ { "action": "add_exclusion", "target": "test_accounts",
                   "payload": { "filter": { "customer.email": { "$regex": "@test\\.example\\.com$" } },
                                "reason": "QA accounts inflate revenue" },
                   "rationale": "Reader asked to exclude test accounts.",
                   "source": "m_6acc..." } ] }
  ],
  "steps": [],                         // explore mode only, see below
  "working_memory": 1,                 // lessons now waiting for the next dream
  "harness_version": 1,                // long-term memory version; unchanged until a dream
  "cost_usd": 0.021
}
```

How the analyst decides:

* A question ("why is Denver down?") gets an answer, checked against the data. Nothing is learned.
* An instruction for the future ("exclude test accounts", "I care about margin", "show me weekly",
  "keep this chart", "stop showing this") becomes a lesson in working memory and is confirmed in the reply.
  It applies at once in every conversation for this company, and becomes permanent at the next dream.
* Something ambiguous gets a clarifying question back instead of a lesson.
* A lesson that does not check out against the data (for example a filter that matches nothing) is not
  kept; the reply says so.

Show each lesson as a chip ("Working memory: Exclude test accounts") with a Forget button
(`DELETE /companies/{id}/memory/{lesson_id}`). Reports do not use working memory until the analyst dreams.
Returns `409` while a reflect or dream job runs for the same company, `404` for an unknown conversation or
report, `400` for an empty message.

### Explore mode replies

In explore mode, `lessons` is always empty and `steps` holds what the analyst showed:

```json
"steps": [
  { "_id": "step_6acc...", "status": "shown", "title": "Revenue by store without test accounts",
    "summary": "Denver's August spike disappears once test accounts are excluded.",
    "vega_lite": { "$schema": "https://vega.github.io/schema/vega-lite/v5.json", "...": "..." },
    "pipeline": { "collection": "sales", "stages": [ "..." ] },
    "chart": { "type": "bar", "x": "store", "y": "revenue", "color": null, "title": "..." },
    "candidate_lesson": { "text": "Exclude test accounts (@test.example.com).", "edits": [ "..." ] },
    "reinforced_lesson": null,         // set when this step repeats something already in working memory
    "conversation_id": "conv_...", "message_id": "m_...", "created_at": "..." }
]
```

Render each step as a card: title, summary, `vegaEmbed(el, step.vega_lite)`, and two buttons, **Remember this**
and **Not useful**. If `candidate_lesson` is set, show its text under the chart ("Would remember: Exclude test
accounts"); without one, keeping the step saves the chart as a view shown in every future report.

### `POST /companies/{id}/explorations/{step_id}/keep` (free)

"Remember this". Adds a strong lesson (strength 3) to working memory, or strengthens the matching one if it is
already there. Returns the lesson. The step's `status` becomes `kept`.

### `POST /companies/{id}/explorations/{step_id}/reject` (free)

"Not useful". The step's `status` becomes `rejected`; if a matching lesson is in working memory it loses 2
strength. Returns `{ "step_id", "status": "rejected", "weakened_lesson": {...} | null }`.

### `GET /conversations/{conversation_id}/steps` (free)

All steps in a conversation, oldest first, with their status (`shown`, `kept`, `rejected`).

### `GET /companies/{id}/conversations`

Newest first: `[{ "_id": "conv_...", "workspace_id", "mode", "report_id", "created_at" }]`.

### `GET /conversations/{conversation_id}/messages`

Oldest first. User messages: `{ "_id", "role": "user", "text", "finding_id", "template_id", "created_at" }`.
Replies: `{ "_id", "role": "assistant", "text", "lessons": [...], "steps": ["step_..."], "cost_usd", "created_at" }`.

---

## Memory and dreaming

### `GET /companies/{id}/memory` (free)

```json
{
  "working": [ { "_id": "lesson_6acc...", "text": "Exclude test accounts (@test.example.com).", "status": "working",
                 "conversation_id": "conv_...", "message_id": "m_...", "edits": [ "..." ], "created_at": "..." } ],
  "long_term": { "version": 1,
                 "counts": { "rules": 0, "focus_metrics": 0, "exclusions": 0, "objectives": 0, "vocabulary": 0,
                             "saved_views_on": 0 },
                 "preferences": { "time_grain": "month", "max_findings": 5, "chart_style": "" } },
  "last_dream": null
}
```

Good for a memory panel: working memory on one side (with Forget buttons), long-term on the other, and the
Dream button between them.

### `GET /companies/{id}/memory/lessons` (free)

Every lesson ever learned, oldest first, with what happened to it: `status` is `working`, `consolidated`
(made permanent), `dropped` (the dream decided not to keep it, see `reason`), `faded` (never reinforced), or
`forgotten` (removed by the user). Each has `strength`, `evidence` (`[{ "kind": "instruction" | "kept" |
"repeated" | "pushback", "ref", "at" }]`), and `dreams_seen`. Consolidated and dropped lessons carry `dream_id`, `outcome`, `reason`, and `consolidated_in_version`.

### `DELETE /companies/{id}/memory/{lesson_id}` (free)

Forget a lesson before it is made permanent. Returns the lesson with `status: "forgotten"`. `404` if it is
not in working memory (already consolidated, dropped, or forgotten).

### `POST /companies/{id}/dream` (costs money, background job)

No body. The analyst reviews the strong lessons in working memory (strength 2 or more), pending comments,
objectives that are off track, and its three most recent reports, then updates long-term memory. Weaker
lessons are not shown to it: they wait, or fade after two dreams without reinforcement. Poll `GET /jobs/{job_id}`; the result is the dream:

```json
{
  "_id": "dream_6acc...",
  "journal": "I learned that test accounts were inflating Denver, so I now exclude them everywhere. I switched to weekly trends as asked. I did not keep the request to hide London coupons as an exclusion; I will stop reporting on that promo instead.",
  "from_version": 1,
  "to_version": 2,
  "lessons_reviewed": 3,
  "waiting": [ { "_id": "lesson_...", "text": "Maybe look at coupons.", "strength": 1 } ],
  "faded":   [ { "_id": "lesson_...", "text": "Split by gender.", "strength": 1 } ],
  "comments_reviewed": 0,
  "decisions": [
    { "source": "lesson_6acc...", "outcome": "applied", "reason": "Holds up: 40 sales match." },
    { "source": "lesson_6acd...", "outcome": "merged",  "reason": "Same as the weekly lesson." },
    { "source": "lesson_6ace...", "outcome": "dropped", "reason": "One-off request." }
  ],
  "edits": [ { "action": "add_exclusion", "target": "test_accounts", "...": "..." } ],
  "diff": [ "+ knowledge.exclusions[test_accounts]: {...}" ],
  "cost_usd": 0.034
}
```

Show the `journal` prominently (it is the analyst explaining what it learned overnight), the `decisions` next
to each lesson, and the `edits` as labeled changes (labels table below). If there is nothing new, the
result has no edits and `journal: "Nothing new to consolidate."`; `to_version` equals `from_version`.
After a dream, run a new report; compare it with the report before the dream to show the effect.

### `GET /companies/{id}/dreams` (free)

Every dream, newest first, same shape as above. A good "dream journal" page.

---

## Comparing report versions

### `GET /reports/compare?a={report_id}&b={report_id}` (free)

Two reports of the same company, in either order (the older one becomes `before`). Shows what the notebook
learned between them and how the findings changed.

```json
{
  "before": { "id": "6acbdd21...", "as_of": "2024-09-30", "harness_version": 1, "summary": "...",
              "applied": { "time_grain": "month", "focus_metrics": [] }, "saved_views": [], "cost_usd": 0.06 },
  "after":  { "id": "6acbdd85...", "as_of": "2024-09-30", "harness_version": 2, "summary": "...",
              "applied": { "time_grain": "week", "focus_metrics": ["margin"] }, "saved_views": [], "cost_usd": 0.08 },
  "same_review_date": true,
  "note": "Same review date: differences come from what the analyst learned (and normal run to run variation).",
  "notebook_changes": [
    { "version": 2, "created_by": "chat", "created_at": "...", "edits": [ { "action": "add_exclusion", "...": "..." } ] }
  ],
  "findings": {
    "in_both": [ { "before": { "id": "denver_spike", "title": "...", "metric": "revenue", "magnitude": 1.5, "...": "..." },
                   "after":  { "id": "denver_margin_decline", "title": "...", "metric": "revenue", "magnitude": -0.37 } } ],
    "new":     [ { "id": "seattle_step_up", "title": "Seattle margin tripled in July...", "...": "..." } ],
    "dropped": [ { "id": "denver_spike", "title": "Denver revenue spiked in Aug 2024...", "...": "..." } ]
  },
  "objectives": [ { "objective_id": "o1", "before": { "value": 44.1, "target": 46, "status": "off_track" },
                    "after": { "value": 46.2, "target": 46, "status": "on_track" } } ]
}
```

Findings are paired when they have the same id, or the same metric on the same segment. For the clearest
before and after, compare two reports with the same `as_of` (the same data), so every difference comes from
what the analyst learned. `400` if the reports belong to different companies, `404` if either does not exist.

A good screen: the two summaries side by side, the notebook changes between them as labeled chips, and the
findings in three groups (new, dropped, changed), with both charts available via `GET /reports/{id}`.

---

## Comments and reflect (batch)

### `POST /companies/{id}/comments`

Free text, or a reaction to a specific chart.

```json
// free text
{ "text": "Exclude test accounts: anything with an @test.example.com email.", "report_id": "6acbdd85..." }

// keep this chart: the analyst saves it as a view shown in every future report
{ "text": "I like this. Show it every time.", "kind": "keep", "finding_id": "denver_margin_decline", "report_id": "6acbdd85..." }

// stop showing a saved view (it is switched off, not deleted)
{ "text": "Stop showing this.", "kind": "stop", "template_id": "weekly_margin_trend" }
```

```json
// 201
{ "id": "c_6acbdd23e86f99a1a46b3cf0", "_id": "c_6acbdd23e86f99a1a46b3cf0", "text": "...", "kind": "comment",
  "finding_id": null, "template_id": null, "workspace_id": "co_retail_trial", "report_id": "6acbdd21...",
  "status": "pending", "created_at": "..." }
```

Comments wait as `pending` until the next reflect. `400` if the text is empty. Good comments are concrete: what to include or exclude, which metric matters, how often, which goal.

### `GET /companies/{id}/comments?status=pending|applied`

Oldest first. Applied comments carry `applied_in_version`, the notebook version they produced.

### `POST /companies/{id}/reflect`: update the notebook (costs money, background job)

No body. Uses all pending comments, plus any goals the latest report marked off track. Needs at least one report first. The job result:

```json
{
  "from_version": 1,
  "to_version": 2,
  "cost_usd": 0.0136,
  "edits": [
    { "action": "add_exclusion", "target": "exclude_test_accounts",
      "payload": { "filter": { "customer.email": { "$regex": "@test\\.example\\.com$" } },
                   "reason": "Test accounts (emails ending @test.example.com) inflate numbers." },
      "rationale": "Owner asked to exclude test accounts by email domain.",
      "source": "c_6acbdd23e86f99a1a46b3cf0" },
    { "action": "set_preference", "target": "time_grain", "payload": { "value": "week" },
      "rationale": "Owner wants week-by-week trends.", "source": "c_6acbdd24e86f99a1a46b3cf2" }
  ],
  "notes": [ { "source": "c_...", "note": "Why a comment could not become a change" } ],
  "diff": [ "+ knowledge.exclusions[exclude_test_accounts]: {...}", "~ preferences.time_grain: month -> week" ]
}
```

For the UI, show `edits` rather than `diff`: each edit says what changed (`action`, `target`), why (`rationale`), and which comment caused it (`source` is the comment id). Suggested labels:

| `action` | Label | Show |
|---|---|---|
| `add_exclusion` / `remove_exclusion` | Now excludes / Stopped excluding | `payload.reason` |
| `set_focus_metric` / `remove_focus_metric` | Now focuses on / Stopped focusing on | `target`, `payload.definition` |
| `add_rule` / `retire_rule` | New rule / Retired rule | `payload.text` |
| `set_preference` | Changed setting | `target` to `payload.value` |
| `set_objective` / `retire_objective` | New goal / Retired goal | `payload.text` |
| `save_template` / `set_template_status` | Saved view / Switched view | `payload.name` or `payload.status` |
| `set_vocab` / `remove_vocab` | Defined term / Dropped term | `target`, `payload.definition` |
| `annotate_collection` | Noted about | `target`, `payload.note` |

If `edits` is empty, `to_version` equals `from_version` and nothing changed; `notes` explains why.

### `GET /companies/{id}/reflections`

Every reflect result, newest first, same shape as above plus `_id`, `workspace_id`, `created_at`.

---

## The notebook (harness)

### `GET /companies/{id}/harness` (optional `?version=3`)

The full notebook. Sections: `identity`, `knowledge` (`collections`, `vocabulary`, `rules`, `focus_metrics`, `exclusions`, `objectives`, `templates`), `capabilities`, `preferences` (`time_grain`, `max_findings`, `chart_style`), `policy`, `output`. Every learned entry has a `source`: the comment or goal id that created it, so the UI can link each line back to who asked for it.

```json
{ "workspace_id": "co_retail_trial", "version": 2, "parent_version": 1,
  "knowledge": {
    "exclusions": [ { "id": "exclude_test_accounts", "filter": { "...": "..." }, "reason": "...", "source": "c_..." } ],
    "focus_metrics": [ { "name": "margin", "definition": "...", "source": "c_..." } ],
    "templates": [ { "id": "weekly_margin_trend", "name": "Weekly gross margin trend", "description": "...",
                     "status": "on", "pipeline": { "...": "..." }, "chart": { "...": "..." }, "source": "c_..." } ],
    "rules": [], "objectives": [], "vocabulary": {}, "collections": {} },
  "preferences": { "time_grain": "week", "max_findings": 5, "chart_style": "" },
  "...": "..." }
```

### `GET /companies/{id}/harness/versions`

The notebook's history, oldest first: `[{ "version", "parent_version", "edits": [...], "created_by", "created_at" }]`. `created_by` is `seed` (blank start), `dream`, `reflect`, `undo:vN#i`, or `revert:vN`. Good for a timeline view.

### `GET /companies/{id}/harness/diff?from_version=1&to_version=2`

```json
{ "from": 1, "to": 2, "changes": [ "+ knowledge.focus_metrics[margin]: {...}", "~ preferences.time_grain: month -> week" ] }
```

Lines start with `+` (added), `-` (removed), or `~` (changed). Technical; use version `edits` for friendly text.

### `POST /companies/{id}/harness/undo`: undo one change in long-term memory (free)

```json
{ "version": 3, "edit_index": 0 }
```

Puts back just the entry that edit touched (for example removes the exclusion it added, or restores the old
time grain), on top of the current notebook, so later changes are kept. Saved as a new version with
`created_by: "undo:v3#0"`. Returns `{ "harness": {...}, "undone": { ...the edit... } }`. `404` if that
version or edit does not exist.

### `POST /companies/{id}/harness/revert`

```json
{ "version": 1 }
```

Makes an earlier version current again by saving a copy of it as the newest version, so history is never lost. Returns the new harness. `404` if that version does not exist.

---

## Simulations (the proof chart)

Scored runs produced by `make simulate`. Read only.

### `GET /simulations`

```json
[ { "company": "co_retail", "run": "r1", "mode": "feedback", "quarters": 9, "updated_at": "..." },
  { "company": "co_retail", "run": "r1", "mode": "baseline", "quarters": 9, "updated_at": "..." } ]
```

### `GET /simulations/{company}/{run}`

```json
{
  "company": "co_retail", "run": "r1",
  "scorecards": [
    { "mode": "feedback", "cycle": 0, "as_of": "2023-12-31", "harness_version": 1, "total": 0.5,
      "recall": { "value": 0.0, "checks": [ { "id": "london_coupon_spike", "type": "story", "passed": false, "detail": "" } ] },
      "compliance": { "value": null, "checks": [] },
      "objectives": { "value": null, "checks": [] },
      "reproducibility": { "value": 1.0, "checks": [ "..." ] },
      "other_findings": 4 }
  ],
  "feedback_rounds": [
    { "cycle": 0, "as_of": "2023-12-31", "comments": ["c01_margin_focus", "..."],
      "reflection": { "from_version": 1, "to_version": 2, "edits": [ "..." ] },
      "before_total": 0.5, "after_total": 0.6667, "kept": true }
  ]
}
```

To draw the proof chart: one line per `mode` (`feedback`, `baseline`), x = `as_of` (one point per quarter), y = `total` (0 to 1). The four parts (`recall` = found what happened, `compliance` = followed feedback, `objectives` = goal numbers right, `reproducibility` = numbers reproducible) each have a `value` (0 to 1, or `null` when there was nothing to score that quarter). `feedback_rounds` says what each round of feedback changed and whether it was `kept`.

---

## Errors

Errors are `{ "detail": "message" }`.

| Status | When |
|---|---|
| `400` | Bad input: empty comment, bad CSV, protected database |
| `404` | Unknown company, report, job, harness version, or simulation |
| `409` | A job is already running for this company (chat is blocked only by reflect or dream) |
| `422` | Body does not match the schema (the response lists which field) |

## TypeScript types

```ts
export type Company = {
  id: string; name: string; company_db: string; source: "existing" | "csv" | "simulation" | "";
  time_fields: Record<string, string>; harness_version: number; created_at: string;
};

export type Job<T = unknown> = {
  _id: string; kind: "report" | "reflect"; workspace_id: string;
  status: "running" | "done" | "failed"; result?: T; error?: string; created_at: string; finished_at?: string;
};

export type ReportSummary = {
  id: string; as_of: string; harness_version: number; created_at: string; cost_usd: number; findings: string[];
};

export type Finding = {
  id: string; title: string; why_it_matters: string; metric: string;
  segment: Record<string, unknown>; period: { start: string; end: string };
  direction: "up" | "down" | "anomaly"; magnitude: number; dollar_impact: number | null;
  pipeline: { collection: string; stages: object[] };
  chart: { type: "line" | "bar" | "area" | "point"; x: string; y: string; color: string | null; title: string };
};

export type ObjectiveStatus = {
  objective_id: string; value: number; target: number;
  status: "on_track" | "off_track" | "met" | "missed"; note: string;
};

export type Report = {
  _id: string; workspace_id: string; harness_version: number; as_of: string; created_at: string;
  cost_usd: number; turns: number;
  report: { summary: string; findings: Finding[]; objectives: ObjectiveStatus[];
            applied: { focus_metrics: string[]; time_grain: string } };
  charts: Record<string, object>;            // Vega-Lite spec per finding id
  template_runs: { id: string; name: string; ok: boolean; error: string }[];
  template_charts: Record<string, object>;   // Vega-Lite spec per saved view id
};

export type Comment = {
  id: string; text: string; kind: "comment" | "keep" | "stop";
  finding_id: string | null; template_id: string | null; report_id: string | null;
  status: "pending" | "applied"; applied_in_version?: number; created_at: string;
};

export type Edit = {
  action: string; target: string; payload: Record<string, unknown>; rationale: string; source: string;
};

export type Lesson = {
  _id: string; text: string; edits: Edit[]; created_at?: string; conversation_id?: string; message_id?: string;
  status: "working" | "consolidated" | "dropped" | "faded" | "forgotten";
  strength: number; dreams_seen?: number;
  evidence?: { kind: "instruction" | "kept" | "repeated" | "pushback"; ref: string; at: string }[];
  dream_id?: string; outcome?: "applied" | "merged" | "dropped"; reason?: string; consolidated_in_version?: number;
};

export type ChatReply = {
  conversation_id: string; mode: "chat" | "explore"; message_id: string; reply_id: string; reply: string;
  lessons: Lesson[]; steps: Step[]; working_memory: number; harness_version: number; cost_usd: number;
};

export type Step = {
  _id: string; status: "shown" | "kept" | "rejected"; title: string; summary: string;
  vega_lite: object; pipeline: { collection: string; stages: object[] }; chart: Finding["chart"];
  candidate_lesson: { text: string; edits: Edit[] } | null; reinforced_lesson?: string | null;
  lesson_id?: string; conversation_id: string; message_id: string; created_at: string;
};

export type Memory = {
  working: Lesson[];
  long_term: { version: number; counts: Record<string, number>; preferences: Record<string, unknown> };
  last_dream: Dream | null;
};

export type Dream = {
  _id: string; journal: string; from_version: number; to_version: number; created_at: string;
  lessons_reviewed: number; comments_reviewed: number;
  waiting: { _id: string; text: string; strength: number }[]; faded: { _id: string; text: string; strength: number }[];
  decisions: { source: string; outcome: "applied" | "merged" | "dropped"; reason: string }[];
  edits: Edit[]; diff: string[]; cost_usd: number;
};

export type Message = {
  _id: string; role: "user" | "assistant"; text: string; created_at: string;
  finding_id?: string | null; template_id?: string | null;
  lessons?: Lesson[]; cost_usd?: number;
};

export type Comparison = {
  before: ReportBrief; after: ReportBrief; same_review_date: boolean; note: string;
  notebook_changes: { version: number; created_by: string; created_at: string; edits: Edit[] }[];
  findings: { in_both: { before: FindingBrief; after: FindingBrief }[]; new: FindingBrief[]; dropped: FindingBrief[] };
  objectives: { objective_id: string; before: GoalBrief | null; after: GoalBrief | null }[];
};
type ReportBrief = { id: string; as_of: string; harness_version: number; created_at: string; cost_usd: number;
  summary: string; applied: { focus_metrics?: string[]; time_grain?: string }; saved_views: string[] };
type FindingBrief = Pick<Finding, "id" | "title" | "metric" | "segment" | "period" | "direction" | "magnitude" | "dollar_impact">;
type GoalBrief = { value: number; target: number; status: ObjectiveStatus["status"] };

export type Reflection = {
  from_version: number; to_version: number; cost_usd: number;
  edits: Edit[]; notes: { source: string; note: string }[]; diff: string[];
};

export type ScorePart = { value: number | null; checks: { id: string; type: string; passed: boolean; detail: string }[] };

export type Scorecard = {
  mode: "feedback" | "baseline"; cycle: number; as_of: string; harness_version: number; total: number;
  recall: ScorePart; compliance: ScorePart; objectives: ScorePart; reproducibility: ScorePart; other_findings: number;
};
```

## Demo tips

* For a clean live demo on the retail data, connect a fresh workspace: `POST /companies {"company_db": "co_retail", "workspace_id": "co_retail_live"}`. It starts with a blank notebook.
* Use `as_of` (for example `2024-09-30`) so the story fits a point in time; the retail data runs from 2021 to 2025.
* A demo arc on retail: run a report (`as_of` `2024-09-30`), chat "Exclude test accounts, anything with an @test.example.com email", "I care about margin, not revenue", "Show me trends week by week", press Dream and show the journal, run a new report with the same `as_of`, then open the comparison.
* A question that shows it answering without learning: "Why did Denver jump in August?"
* An explore arc: start a conversation with `mode: "explore"`, ask a few what-ifs ("revenue by store without test accounts", "margin by product", "weekly online satisfaction"), press Remember this on two steps and Not useful on one, then Dream. The journal and `waiting` / `faded` lists show that only what landed became long-term memory.
