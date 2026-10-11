"""How the analyst writes for people. Simple by default; a reader can ask for more detail and the analyst
learns it as the `writing_style` preference.

Examples here stay business neutral: the analyst learns each company's own words from the data.
"""

import re

GUIDES = {
    "simple": (
        "Write so a smart high school student or a busy owner understands it on the first read:\n"
        "- Short sentences and everyday words. One idea per sentence.\n"
        "- Name things in full: 'profit at our West region locations', not 'West margin'.\n"
        "- Say what happened with plain verbs: went up, went down, stayed flat, jumped, dropped. Not "
        "'trending', 'declining since peak', 'mix shift'.\n"
        "- Prefer the plainest word (profit, revenue, customers). If a business term is needed, explain it once in a "
        "few words, e.g. 'profit margin (the share of each dollar we keep after costs)'.\n"
        "- Round numbers and say them simply: 'about $11,000', 'down about a third', never '-0.369'.\n"
        "- Titles and headlines are short full sentences that say what happened and why it matters.\n"
        "- No abbreviations (YoY, MoM, SKU), no codes, no field names.\n"
        "Example. Too technical: 'West margin falling since July peak, driven by mix'. Simple: 'Our West region is "
        "making less profit each month since July, mostly because we sell more low-profit products.'"
    ),
    "standard": (
        "Write in clear business language for a manager: common business terms (margin, conversion, average order "
        "value) are fine without explanation; keep sentences short; round numbers; no codes or field names."
    ),
    "technical": (
        "Write for an analyst: precise terms, exact figures with units and comparison periods, and a short note on "
        "method where it matters. Still no raw field names in titles or charts."
    ),
}

# Shorthand that has no place in simple writing. Checked in titles, headlines, and focus text.
JARGON = re.compile(
    r"\b(yoy|y/y|qoq|mom|wow|sku|aov|cagr|delta|run-rate|trailing|mix shift|peak|baseline|wk|vs)\b",
    re.IGNORECASE,
)


def guide(style: str) -> str:
    return GUIDES.get(style, GUIDES["simple"])


def jargon(text: str) -> list[str]:
    return sorted({m.group(0).lower() for m in JARGON.finditer(text)})
