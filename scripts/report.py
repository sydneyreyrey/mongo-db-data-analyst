"""Run Analyze mode once for a company and save the report.

make report COMPANY=co_retail
"""

import argparse
import asyncio
import html
import json
from pathlib import Path

from analyst import reports
from analyst.agent import analyze
from analyst.data import CompanyData
from common import llm
from common.db import get_client, get_db
from common.settings import get_settings
from harness import store

OUT = Path(__file__).resolve().parent.parent / "out"


def write_html(doc: dict, path: Path) -> None:
    r = doc["report"]
    cards = []
    for i, f in enumerate(r["findings"]):
        impact = f" · ${f['dollar_impact']:,.0f}" if f.get("dollar_impact") else ""
        cards.append(
            f"<section><h2>{i + 1}. {html.escape(f['title'])}</h2>"
            f"<p class=meta>{html.escape(f['metric'])} · {f['direction']} {f['magnitude']:+.0%}"
            f" · {f['period']['start']} to {f['period']['end']}{impact}</p>"
            f"<p>{html.escape(f['why_it_matters'])}</p><div id=c{i} class=chart></div></section>"
        )
    specs = json.dumps([doc["charts"][f["id"]] for f in r["findings"]])
    path.write_text(f"""<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<title>{html.escape(doc["workspace_id"])} report v{doc["harness_version"]}</title>
<script src="https://cdn.jsdelivr.net/npm/vega@5.30.0"></script>
<script src="https://cdn.jsdelivr.net/npm/vega-lite@5.21.0"></script>
<script src="https://cdn.jsdelivr.net/npm/vega-embed@6.26.0"></script>
<style>body{{font:15px/1.5 system-ui;max-width:860px;margin:2rem auto;padding:0 16px;color:#222}}
section{{border-top:1px solid #ddd;padding:1rem 0}}.meta{{color:#666;font-size:13px}}.chart{{width:100%}}</style>
<h1>{html.escape(doc["workspace_id"])}: analyst report (harness v{doc["harness_version"]})</h1>
<p>{html.escape(r["summary"])}</p>{"".join(cards)}
<p class=meta>{doc["turns"]} turns · ${doc["cost_usd"]:.2f}</p>
<script>{specs}.forEach((s,i)=>vegaEmbed('#c'+i,s,{{actions:false}}));</script>
""")


async def main(company: str) -> None:
    settings = get_settings()
    db = get_db()
    harness = await store.create_workspace(db, company, company)
    data = CompanyData(get_client()[company], harness)
    meter = llm.CostMeter(limit_usd=settings.max_usd)

    print(f"Analyzing {company} with harness v{harness.version} ({settings.analyst_model})...")
    analysis = await analyze(harness, data, company, meter)
    report_id = await reports.save(db, company, harness.version, analysis)

    tools = [s.tool for s in analysis.steps]
    errors = sum(not s.ok for s in analysis.steps)
    print(f"\nDone in {analysis.turns} turns, {len(tools)} tool calls ({errors} errors), ${analysis.cost_usd:.2f}")
    print(f"Saved report {report_id}\n\n{analysis.report.summary}\n")
    for i, f in enumerate(analysis.report.findings, 1):
        print(f"{i}. {f.title}  [{f.metric} {f.direction} {f.magnitude:+.0%}, {f.period.start} to {f.period.end}]")

    OUT.mkdir(exist_ok=True)
    path = OUT / f"{company}_v{harness.version}.html"
    write_html(await reports.latest(db, company), path)
    print(f"\nHTML: {path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--company", required=True)
    asyncio.run(main(parser.parse_args().company))
