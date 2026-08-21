"""Diff helpers: compare two plain dicts (comparable fields only).

LiteLLM injects server defaults & runtime metrics into read responses
(e.g. `spend`, `updated_at`, `budget_reset_at`, `tpm_limit_type`, `mode`
inference). Reconciling on those would cause perpetual drift, so we diff
only a curated set of *manageable* fields per resource.
"""

from __future__ import annotations

from typing import Any


def _equiv(a: Any, b: Any) -> bool:
    """Field equivalence: exact equality plus empty-collection tolerance.

    LiteLLM always echoes collection-shaped fields as (possibly empty)
    containers — `models: []`, `allowed_routes: []`, `guardrails_add: []`,
    `guardrail_info: {}` — even when the spec omits them. Treat an omitted
    spec field (None) as equivalent to an empty list OR empty dict so that
    doesn't read as perpetual drift.
    """
    if a == b:
        return True
    if a is None and (b == [] or b == {}):
        return True
    return bool(b is None and (a == [] or a == {}))


def comparable_diff(
    desired: dict[str, Any], live: dict[str, Any], fields: list[str]
) -> dict[str, tuple[Any, Any]]:
    """Return {field: (desired_val, live_val)} for fields that differ.

    Only fields listed in `fields` are compared; missing live fields are
    treated as None (which flags a diff if desired has a value). Empty-list
    fields are considered equal to an omitted (None) counterpart.
    """
    changes: dict[str, tuple[Any, Any]] = {}
    for f in fields:
        want = desired.get(f)
        have = live.get(f)
        if not _equiv(want, have):
            changes[f] = (want, have)
    return changes
