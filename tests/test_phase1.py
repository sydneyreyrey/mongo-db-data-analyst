import json
from pathlib import Path

import pytest

from common.settings import get_settings

ROOT = Path(__file__).resolve().parent.parent
FORBIDDEN = ("bench", "planted", "comments.json")


def agent_sources():
    for package in ("analyst", "api"):
        yield from (ROOT / package).rglob("*.py")


def test_agent_code_cannot_reference_answer_key():
    """Invariant 2: nothing under analyst/ or api/ may point at bench/ files."""
    offenders = [
        f"{path.relative_to(ROOT)}: {word}"
        for path in agent_sources()
        for word in FORBIDDEN
        if word in path.read_text()
    ]
    assert not offenders, offenders


def test_answer_key_files_are_valid():
    planted = json.loads((ROOT / "bench/co_retail/planted.json").read_text())
    comments = json.loads((ROOT / "bench/co_retail/comments.json").read_text())
    ids = {f["id"] for f in planted["findings"]}
    assert len(ids) == 6
    for c in comments["comments"]:
        assert c["text"] and c["check"]["type"]
        if c["check"]["type"] == "suppress_finding":
            assert c["check"]["planted_id"] in ids


@pytest.mark.skipif(not get_settings().mongodb_uri, reason="MONGODB_URI not set")
def test_rebuild_is_deterministic():
    from scripts.build_company import build

    planted = json.loads((ROOT / "bench/co_retail/planted.json").read_text())
    assert build(write=False) == planted["content_hash"]
