"""Render a harness into the analyst's system prompt, in slot order.

The fixed instructions come first, then the harness. The whole system prompt only changes when the
harness version changes, so it carries one cache breakpoint at the end.
"""

import json

from harness.schema import Harness

BASE_INSTRUCTIONS = (
    "You are an autonomous data analyst connected read only to one company's MongoDB database.\n"
    "Explore the data with your tools, decide what is most important for the business, and report it.\n"
    "Follow the company playbook below. It was built from this user's earlier feedback "
    "and overrides your defaults."
)


def _section(title: str, lines: list[str]) -> str:
    return f"## {title}\n" + "\n".join(lines) if lines else ""


def render(h: Harness) -> list[dict]:
    k = h.knowledge
    sections = [
        _section(
            "Identity",
            [f"Role: {h.identity.role}", f"Audience: {h.identity.audience}"] + [f"- {v}" for v in h.identity.values],
        ),
        _section("Collections", [f"- {name}: {note}" for name, note in sorted(k.collections.items())]),
        _section("Vocabulary", [f"- {term}: {t.definition}" for term, t in sorted(k.vocabulary.items())]),
        _section("Focus metrics", [f"- {m.name}: {m.definition}" for m in k.focus_metrics]),
        _section(
            "Exclusions (apply to every query)",
            [f"- {e.id}: {json.dumps(e.filter, sort_keys=True)} ({e.reason})" for e in k.exclusions],
        ),
        _section("Rules", [f"- {r.text}" for r in k.rules]),
        _section(
            "Preferences",
            [
                f"- Time grain: {h.preferences.time_grain}",
                f"- At most {h.preferences.max_findings} findings",
            ]
            + ([f"- Chart style: {h.preferences.chart_style}"] if h.preferences.chart_style else []),
        ),
        _section(
            "Policy",
            [
                h.policy.scope,
                f"- Queries return at most {h.policy.constraints.max_docs} documents "
                f"and time out after {h.policy.constraints.max_time_ms} ms.",
            ],
        ),
        _section("Output", [h.output.form]),
    ]
    playbook = "\n\n".join(s for s in sections if s)
    return [
        {"type": "text", "text": BASE_INSTRUCTIONS},
        {"type": "text", "text": playbook, "cache_control": {"type": "ephemeral"}},
    ]
