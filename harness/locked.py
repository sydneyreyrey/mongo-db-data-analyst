"""Fields that Reflect mode may never change."""

from typing import Any

from harness.schema import Harness

LOCKED_PATHS: tuple[str, ...] = (
    "workspace_id",
    "identity.values",
    "policy.scope",
    "policy.constraints",
)


class LockedFieldError(ValueError):
    pass


def _get(data: dict[str, Any], path: str) -> Any:
    for part in path.split("."):
        data = data[part]
    return data


def is_locked(path: str) -> bool:
    return any(path == p or path.startswith(p + ".") or p.startswith(path + ".") for p in LOCKED_PATHS)


def assert_locked_unchanged(before: Harness, after: Harness) -> None:
    """Post-condition check on every edit: locked values must be identical."""
    old, new = before.model_dump(), after.model_dump()
    for path in LOCKED_PATHS:
        if _get(old, path) != _get(new, path):
            raise LockedFieldError(f"Edit changed locked field {path!r}")
