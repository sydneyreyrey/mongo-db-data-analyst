import pytest

from analyst.prompt_builder import render
from common.settings import get_settings
from harness import store
from harness.diff import diff
from harness.edits import Edit, InvalidEdit, apply_edit
from harness.locked import LockedFieldError
from harness.schema import Harness
from harness.seed import seed_harness


def edit(action, target, payload=None, comment="c1"):
    return Edit(action=action, target=target, payload=payload or {}, rationale="test", comment_id=comment)


@pytest.fixture
def base() -> Harness:
    h = seed_harness("ws")
    h = apply_edit(h, edit("add_rule", "r1", {"text": "old rule"}))
    h = apply_edit(h, edit("set_vocab", "aov", {"definition": "average order value"}))
    h = apply_edit(h, edit("set_focus_metric", "revenue", {"definition": "sum of price x qty"}))
    h = apply_edit(
        h, edit("add_exclusion", "x1", {"filter": {"storeLocation": "X"}, "reason": "closed store"})
    )
    return h


# Every action, with the diff line it must produce.
CASES = [
    (edit("annotate_collection", "sales", {"note": "one doc per sale"}), "+ knowledge.collections.sales"),
    (edit("set_vocab", "margin", {"definition": "price minus cost"}), "+ knowledge.vocabulary.margin"),
    (edit("remove_vocab", "aov"), "- knowledge.vocabulary.aov"),
    (edit("add_rule", "r2", {"text": "Exclude refunds"}), "+ knowledge.rules[r2]"),
    (edit("retire_rule", "r1"), "- knowledge.rules[r1]"),
    (
        edit("set_focus_metric", "margin", {"definition": "sum((price-cost) x qty)"}),
        "+ knowledge.focus_metrics[margin]",
    ),
    (edit("remove_focus_metric", "revenue"), "- knowledge.focus_metrics[revenue]"),
    (
        edit(
            "add_exclusion",
            "test_accounts",
            {"filter": {"customer.email": {"$regex": "@test\\.example\\.com$"}}, "reason": "QA data"},
        ),
        "+ knowledge.exclusions[test_accounts]",
    ),
    (edit("remove_exclusion", "x1"), "- knowledge.exclusions[x1]"),
    (edit("set_preference", "time_grain", {"value": "week"}), "~ preferences.time_grain: month -> week"),
]


@pytest.mark.parametrize(("e", "expected"), CASES, ids=[c[0].action for c in CASES])
def test_each_action_makes_new_version_with_diff(base, e, expected):
    new = apply_edit(base, e)
    assert new.version == base.version + 1
    assert new.parent_version == base.version
    lines = diff(base, new)
    assert len(lines) == 1 and lines[0].startswith(expected), lines


def test_edits_record_their_source_comment(base):
    new = apply_edit(base, edit("add_rule", "r9", {"text": "x"}, comment="c42"))
    assert new.knowledge.rules[-1].source_comment == "c42"


def test_apply_does_not_mutate_input(base):
    before = base.model_dump()
    apply_edit(base, edit("add_rule", "r2", {"text": "x"}))
    assert base.model_dump() == before


@pytest.mark.parametrize(
    "target", ["identity.values", "policy.scope", "policy.constraints.max_time_ms", "policy", "workspace_id"]
)
def test_locked_targets_raise(base, target):
    with pytest.raises(LockedFieldError):
        apply_edit(base, edit("set_preference", target, {"value": "x"}))


@pytest.mark.parametrize(
    "e",
    [
        edit("set_preference", "time_grain", {"value": "fortnight"}),
        edit("set_preference", "max_findings", {"value": 50}),
        edit("set_preference", "colour", {"value": "red"}),
        edit("remove_vocab", "missing"),
        edit("retire_rule", "missing"),
        edit("add_rule", "r1", {"text": "duplicate id"}),
        edit("add_rule", "r3", {}),
        edit("add_exclusion", "bad", {"filter": {"$where": "1"}, "reason": "x"}),
        edit("add_exclusion", "bad", {"filter": {"a": {"$lookup": {}}}, "reason": "x"}),
        edit("add_exclusion", "bad", {"filter": "not a dict", "reason": "x"}),
    ],
    ids=lambda e: f"{e.action}:{e.target}",
)
def test_invalid_edits_raise(base, e):
    with pytest.raises(InvalidEdit):
        apply_edit(base, e)


def test_prompt_renders_in_slot_order_with_cache_breakpoint(base):
    blocks = render(base)
    assert blocks[-1]["cache_control"] == {"type": "ephemeral"}
    text = blocks[-1]["text"]
    order = [
        "## Identity",
        "## Vocabulary",
        "## Focus metrics",
        "## Exclusions",
        "## Rules",
        "## Preferences",
        "## Policy",
        "## Output",
    ]
    positions = [text.index(h) for h in order]
    assert positions == sorted(positions)
    assert "## Collections" not in text  # empty sections are omitted


def test_prompt_is_stable_for_same_version(base):
    assert render(base) == render(base.model_copy(deep=True))


@pytest.mark.skipif(not get_settings().mongodb_uri, reason="MONGODB_URI not set")
async def test_store_round_trip_in_atlas():
    from common.db import get_client

    db = get_client()[get_settings().mongodb_db + "_test"]
    await db.drop_collection("workspaces")
    await db.drop_collection("harness_versions")
    try:
        v1 = await store.create_workspace(db, "ws_test", "co_retail")
        assert v1.version == 1
        assert (await store.create_workspace(db, "ws_test", "co_retail")).version == 1  # idempotent

        round1 = [
            edit(
                "add_exclusion",
                "test_accounts",
                {"filter": {"customer.email": {"$regex": "@test\\.example\\.com$"}}, "reason": "QA"},
            ),
            edit("set_focus_metric", "margin", {"definition": "sum((price-cost) x qty)"}, comment="c2"),
        ]
        v2 = await store.apply_edits(db, v1, round1)
        assert v2.version == 2 and v2.parent_version == 1
        assert (await store.latest(db, "ws_test")).knowledge.exclusions[0].filter == {
            "customer.email": {"$regex": "@test\\.example\\.com$"}
        }
        assert len(await store.version_diff(db, "ws_test", 1, 2)) == 2

        with pytest.raises(InvalidEdit):  # a bad edit in a round saves nothing
            await store.apply_edits(
                db, v2, [edit("add_rule", "r", {"text": "ok"}), edit("retire_rule", "nope")]
            )
        assert (await store.latest(db, "ws_test")).version == 2

        v3 = await store.revert_to(db, "ws_test", 1)
        assert v3.version == 3 and v3.knowledge.exclusions == []
        assert [h["created_by"] for h in await store.history(db, "ws_test")] == [
            "seed",
            "reflect",
            "revert:v2",
        ]
    finally:
        await db.client.drop_database(db.name)
