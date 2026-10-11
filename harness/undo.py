"""Undo one learned change: restore the entry it touched to how it was before that change.

This is a human action, like a revert. The result is saved as a new version so history is never lost, and
locked fields are checked exactly as for edits.
"""

from harness.edits import Edit
from harness.locked import assert_locked_unchanged
from harness.schema import Harness

# Where each action's entry lives: (container path, how list items are keyed). None key means a dict.
LOCATIONS: dict[str, tuple[str, str | None]] = {
    "annotate_collection": ("knowledge.collections", None),
    "set_vocab": ("knowledge.vocabulary", None),
    "remove_vocab": ("knowledge.vocabulary", None),
    "add_rule": ("knowledge.rules", "id"),
    "retire_rule": ("knowledge.rules", "id"),
    "set_focus_metric": ("knowledge.focus_metrics", "name"),
    "remove_focus_metric": ("knowledge.focus_metrics", "name"),
    "add_exclusion": ("knowledge.exclusions", "id"),
    "remove_exclusion": ("knowledge.exclusions", "id"),
    "set_objective": ("knowledge.objectives", "id"),
    "retire_objective": ("knowledge.objectives", "id"),
    "save_template": ("knowledge.templates", "id"),
    "set_template_status": ("knowledge.templates", "id"),
}


def _container(data: dict, path: str):
    for part in path.split("."):
        data = data[part]
    return data


def undo_edit(current: Harness, before: Harness, edit: Edit) -> Harness:
    """Return `current` with the entry `edit` touched put back the way it was in `before`."""
    cur, old = current.model_dump(), before.model_dump()
    t = edit.target
    if edit.action == "set_preference":
        cur["preferences"][t] = old["preferences"][t]
    elif edit.action in ("enable_tool", "disable_tool"):
        tools = [x for x in cur["capabilities"]["tools_enabled"] if x != t]
        if t in old["capabilities"]["tools_enabled"]:
            tools.append(t)
        cur["capabilities"]["tools_enabled"] = tools
    else:
        path, key = LOCATIONS[edit.action]
        now, then = _container(cur, path), _container(old, path)
        if key is None:
            now.pop(t, None)
            if t in then:
                now[t] = then[t]
        else:
            previous = next((x for x in then if x[key] == t), None)
            kept = [x for x in now if x[key] != t]
            now.clear()
            now.extend(kept + ([previous] if previous else []))
    restored = Harness.model_validate(cur)
    restored.parent_version = current.version
    restored.version = current.version + 1
    assert_locked_unchanged(current, restored)
    return restored
