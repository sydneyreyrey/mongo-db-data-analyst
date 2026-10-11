"""Build the proof page for a simulation run: score by quarter, with feedback vs baseline.

    make proof COMPANY=co_retail RUN=r1   # writes out/<company>_<run>_proof.html

Reads only what the simulator stored in MongoDB (`scorecards`, `cycles`, `reports`, `reflections`).
"""

import argparse
import asyncio
import html
import json
from pathlib import Path

from common.db import get_db

OUT = Path(__file__).resolve().parent.parent / "out"
PARTS = ("recall", "compliance", "objectives", "reproducibility")
PART_LABELS = {
    "recall": "Found what happened",
    "compliance": "Followed feedback",
    "objectives": "Goal numbers right",
    "reproducibility": "Numbers reproducible",
}
MODES = {"feedback": "With feedback", "baseline": "No feedback"}
COLORS = {"light": ["#2a78d6", "#eb6834"], "dark": ["#3987e5", "#d95926"]}


VERBS = {
    "annotate_collection": "Noted about", "set_vocab": "Defined term", "remove_vocab": "Dropped term",
    "add_rule": "New rule", "retire_rule": "Retired rule", "set_focus_metric": "Now focuses on",
    "remove_focus_metric": "Stopped focusing on", "add_exclusion": "Now excludes",
    "remove_exclusion": "Stopped excluding",
    "set_preference": "Changed setting", "set_objective": "New goal", "retire_objective": "Retired goal",
    "save_template": "Saved view", "set_template_status": "Switched view", "enable_tool": "Enabled tool",
    "disable_tool": "Disabled tool",
}  # fmt: skip


def describe(edit: dict) -> str:
    """One plain sentence per notebook change: what changed, then why, in the analyst's words."""
    payload = edit.get("payload", {})
    what = {
        "set_preference": f"{edit['target'].replace('_', ' ')} to {payload.get('value')}",
        "set_template_status": f"{edit['target'].replace('_', ' ')} {payload.get('status')}",
        "add_rule": payload.get("text", edit["target"]),
        "set_objective": payload.get("text", edit["target"]),
        "save_template": payload.get("name", edit["target"]),
    }.get(edit["action"], edit["target"].replace("_", " "))
    sentence = f"{VERBS.get(edit['action'], edit['action'])}: {what}"
    return sentence if len(sentence) <= 220 else sentence[:217] + "..."


def quarter(as_of: str) -> str:
    year, month = int(as_of[:4]), int(as_of[5:7])
    return f"Q{(month - 1) // 3 + 1} {year}"


async def load(company: str, run: str) -> dict:
    db = get_db()
    cards = await db.scorecards.find({"company": company, "run": run, "stage": "review"}, sort=[("cycle", 1)]).to_list()
    cycles = await db.cycles.find({"run": run, "mode": "feedback", "workspace_id": {"$regex": f"^{company}_sim_"}},
                                  sort=[("cycle", 1)]).to_list()  # fmt: skip
    costs = {}
    for mode in MODES:
        ws = f"{company}_sim_{mode}_{run}"
        costs[mode] = sum([d.get("cost_usd", 0) async for d in db.reports.find({"workspace_id": ws})])
        costs[mode] += sum([d.get("cost_usd", 0) async for d in db.reflections.find({"workspace_id": ws})])
    return {"cards": cards, "cycles": cycles, "costs": costs}


def series(cards: list[dict]) -> tuple[list[dict], list[dict]]:
    totals, parts = [], []
    for c in cards:
        q, mode = quarter(c["as_of"]), MODES[c["mode"]]
        totals.append({"quarter": q, "order": c["cycle"], "mode": mode, "score": round(100 * c["total"]),
                       "version": c["harness_version"]})  # fmt: skip
        for p in PARTS:
            value = c[p]["value"]
            if value is not None:
                parts.append({"quarter": q, "order": c["cycle"], "mode": mode, "part": PART_LABELS[p],
                              "score": round(100 * value)})  # fmt: skip
    return totals, parts


def spec(values: list[dict], height: int, title: str = "", facet: bool = False) -> dict:
    x = {"field": "quarter", "type": "ordinal", "sort": {"field": "order"}, "title": None,
         "axis": {"labelAngle": 0, "labelFontSize": 11}}  # fmt: skip
    y = {"field": "score", "type": "quantitative", "title": "Score", "scale": {"domain": [0, 100]},
         "axis": {"tickCount": 5, "grid": True}}  # fmt: skip
    color = {"field": "mode", "type": "nominal", "title": None, "sort": list(MODES.values()),
             "legend": {"orient": "top", "direction": "horizontal"}}  # fmt: skip
    tooltip = [{"field": "quarter"}, {"field": "mode", "title": "Run"}, {"field": "score", "title": "Score"}]
    layer = {
        "layer": [
            {"mark": {"type": "line", "strokeWidth": 2, "interpolate": "monotone"}},
            {"mark": {"type": "point", "filled": True, "size": 64, "strokeWidth": 2}},
            {
                "mark": {"type": "rule", "opacity": 0},
                "params": [
                    {
                        "name": "hover",
                        "select": {
                            "type": "point",
                            "fields": ["quarter"],
                            "on": "pointerover",
                            "nearest": True,
                            "clear": "pointerout",
                        },
                    }
                ],
                "encoding": {
                    "opacity": {"condition": {"param": "hover", "empty": False, "value": 0.35}, "value": 0},
                    "y": {},
                },
            },  # fmt: skip
        ],
        "encoding": {"x": x, "y": y, "color": color, "tooltip": tooltip},
    }
    base = {"$schema": "https://vega.github.io/schema/vega-lite/v5.json", "data": {"values": values}}
    if facet:
        facet_by = {"field": "part", "type": "nominal", "title": None, "sort": list(PART_LABELS.values())}
        return {**base, "facet": facet_by,
                "columns": 2, "spec": {**layer, "width": "container", "height": height}, "title": title,
                "resolve": {"axis": {"x": "independent"}}}  # fmt: skip
    else:
        last = [v for v in values if v["order"] == max(x["order"] for x in values)]
        layer["layer"].append({
            "data": {"values": last},
            "mark": {"type": "text", "align": "left", "dx": 10, "fontSize": 12, "fontWeight": 600},
            "encoding": {"x": x, "y": y, "text": {"field": "score"}, "color": {**color, "legend": None}},
        })  # fmt: skip
    return {**base, **layer, "width": "container", "height": height, "title": title}


def page(company: str, run: str, data: dict) -> str:
    totals, parts = series(data["cards"])
    by_mode = {m: [t for t in totals if t["mode"] == label] for m, label in MODES.items()}
    avg = {m: (sum(t["score"] for t in rows) / len(rows) if rows else 0) for m, rows in by_mode.items()}
    final = {m: (rows[-1]["score"] if rows else 0) for m, rows in by_mode.items()}
    kept = sum(1 for c in data["cycles"] if c.get("kept"))
    reverted = sum(1 for c in data["cycles"] if c.get("kept") is False)

    rows = []
    fb = {t["quarter"]: t for t in by_mode["feedback"]}
    bl = {t["quarter"]: t for t in by_mode["baseline"]}
    decisions = {quarter(c["as_of"]): c for c in data["cycles"]}
    for q in sorted({*fb, *bl}, key=lambda q: (q[-4:], q[1])):
        c = decisions.get(q)
        if not c:
            change = "No feedback this quarter"
        elif not c["reflection"]["edits"]:
            change = "Feedback gave no notebook changes"
        else:
            verdict = "kept" if c.get("kept") else "undone"
            edits = c["reflection"]["edits"]
            lines = "".join(f"<li>{html.escape(describe(e))}</li>" for e in edits)
            noun = "change" if len(edits) == 1 else "changes"
            change = f"{len(edits)} {noun}, {verdict}<ul>{lines}</ul>"
        rows.append(
            f"<tr><td>{q}</td><td class=num>{bl.get(q, {}).get('score', '')}</td>"
            f"<td class=num>{fb.get(q, {}).get('score', '')}</td><td>{change}</td></tr>"
        )

    specs = {"main": spec(totals, 300)}
    for i, label in enumerate(PART_LABELS.values()):
        values = [p for p in parts if p["part"] == label]
        if values:
            small = spec(values, 150, title=label)
            small["encoding"]["x"]["axis"] = {"labelAngle": -45, "labelFontSize": 10,
                                              "labelExpr": "replace(datum.label, ' 20', \" '\")"}  # fmt: skip
            small["encoding"]["color"]["legend"] = None
            specs[f"part{i}"] = small
    part_divs = "".join(f"<div id={k} class=chart></div>" for k in specs if k.startswith("part"))
    tiles = [
        ("Average score, with feedback", f"{avg['feedback']:.0f}", f"vs {avg['baseline']:.0f} with no feedback"),
        ("Final quarter", f"{final['feedback']}", f"vs {final['baseline']} with no feedback"),
        ("Notebook updates", f"{kept} kept", f"{reverted} undone for scoring worse"),
        ("Cost of both runs", f"${data['costs']['feedback'] + data['costs']['baseline']:.2f}",
         f"feedback ${data['costs']['feedback']:.2f}, baseline ${data['costs']['baseline']:.2f}"),
    ]  # fmt: skip
    tile_html = "".join(
        f"<div class=tile><div class=label>{a}</div><div class=value>{b}</div><div class=sub>{c}</div></div>"
        for a, b, c in tiles
    )
    return f"""<!doctype html><html lang=en><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<title>Recursive Analyst proof</title>
<script src="https://cdn.jsdelivr.net/npm/vega@5.30.0"></script>
<script src="https://cdn.jsdelivr.net/npm/vega-lite@5.21.0"></script>
<script src="https://cdn.jsdelivr.net/npm/vega-embed@6.26.0"></script>
<style>
:root{{--bg:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--line:#e4e3de;--tile:#f4f3ef}}
@media (prefers-color-scheme: dark){{:root:not([data-theme=light]){{--bg:#1a1a19;--ink:#fff;--ink2:#c3c2b7;
--line:#34332f;--tile:#23231f}}}}
body{{background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,sans-serif;max-width:980px;margin:0 auto;
padding:24px 16px}}
h1{{font-size:26px;margin:0 0 4px}}p.lede{{color:var(--ink2);margin:0 0 20px}}
.tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin:0 0 24px}}
.tile{{background:var(--tile);border-radius:10px;padding:14px 16px}}.label,.sub{{color:var(--ink2);font-size:13px}}
.value{{font-size:30px;font-weight:650;font-variant-numeric:tabular-nums}}
h2{{font-size:17px;margin:28px 0 8px}}.chart{{width:100%}}
.parts{{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:16px}}td:first-child{{white-space:nowrap}}
table{{width:100%;border-collapse:collapse;font-size:14px}}td,th{{border-top:1px solid var(--line);padding:8px 6px;
text-align:left;vertical-align:top}}.num{{text-align:right;font-variant-numeric:tabular-nums}}
ul{{margin:4px 0 0;padding-left:18px;color:var(--ink2);font-size:13px}}
</style>
<h1>Does feedback make the analyst better?</h1>
<p class=lede>{html.escape(company)}, run {html.escape(run)}. The same analyst reviews nine quarters twice: once with a
manager's comments and goals, once with none. Every report is graded by plain code against a hidden answer key.</p>
<div class=tiles>{tile_html}</div>
<h2>Score by quarter (0 to 100)</h2><div id=main class=chart></div>
<h2>What the score is made of</h2>
<p class=lede>Blue is with feedback, orange is no feedback.</p><div class=parts>{part_divs}</div>
<h2>Quarter by quarter</h2>
<table><tr><th>Quarter</th><th class=num>No feedback</th><th class=num>With feedback</th><th>Notebook changes from that
quarter's feedback</th></tr>{"".join(rows)}</table>
<script>
const specs = {json.dumps(specs)};
const dark = matchMedia('(prefers-color-scheme: dark)').matches;
const colors = {json.dumps(COLORS)}[dark ? 'dark' : 'light'];
const ink = dark ? '#c3c2b7' : '#52514e', grid = dark ? '#34332f' : '#e4e3de';
const config = {{background: null, view: {{stroke: null}}, range: {{category: colors}},
  axis: {{labelColor: ink, titleColor: ink, gridColor: grid, domainColor: grid, tickColor: grid}},
  legend: {{labelColor: ink}}, header: {{labelColor: ink, labelFontSize: 13}}, title: {{color: ink}},
  text: {{color: ink}}}};
for (const [id, s] of Object.entries(specs)) vegaEmbed('#' + id, {{...s, config}}, {{actions: false}});
</script>
</html>
"""


async def main(company: str, run: str) -> None:
    data = await load(company, run)
    if not data["cards"]:
        raise SystemExit(f"No scorecards for {company} run {run}. Run make simulate first.")
    OUT.mkdir(exist_ok=True)
    path = OUT / f"{company}_{run}_proof.html"
    path.write_text(page(company, run, data))
    print(path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--company", required=True)
    parser.add_argument("--run", default="r1")
    args = parser.parse_args()
    asyncio.run(main(args.company, args.run))
