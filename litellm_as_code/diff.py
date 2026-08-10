"""Diff helpers: compare two plain dicts (comparable fields only).

LiteLLM injects server defaults & runtime metrics into read responses
(e.g. `spend`, `updated_at`, `budget_reset_at`, `tpm_limit_type`, `mode`
inference). Reconciling on those would cause perpetual drift, so we diff
only a curated set of *manageable* fields per resource.
"""

from __future__ import annotations

from typing import Any


def comparable_diff(
    desired: dict[str, Any], live: dict[str, Any], fields: list[str]
) -> dict[str, tuple[Any, Any]]:
    """Return {field: (desired_val, live_val)} for fields that differ.

    Only fields listed in `fields` are compared; missing live fields are
    treated as None (which flags a diff if desired has a value).
    """
    changes: dict[str, tuple[Any, Any]] = {}
    for f in fields:
        want = desired.get(f)
        have = live.get(f)
        if want != have:
            changes[f] = (want, have)
    return changes
