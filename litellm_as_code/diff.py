"""Diff helpers: compare two plain dicts (comparable fields only).

LiteLLM injects server defaults & runtime metrics into read responses
(e.g. `spend`, `updated_at`, `budget_reset_at`, `tpm_limit_type`, `mode`
inference). Reconciling on those would cause perpetual drift, so we diff
only a curated set of *manageable* fields per resource.

Fields the proxy accepts but does not PERSIST are handled separately, after
the apply, by the generic drift classification
(litellm_as_code/classification.py, issue #29) — never here: at diff time a
non-persisting field is still indistinguishable from real drift.
"""

from __future__ import annotations

from typing import Any


def equiv(a: Any, b: Any) -> bool:
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

    Deliberately no scalar-default tolerance (issue #12): on the pinned proxy
    (v1.97.0, probed live) unset budget-table limits (max_budget, soft_budget,
    tpm_limit, rpm_limit), unset team `max_budget` and unset key `max_budget`
    all read back as `None` — not `0`/`0.0` — so the empty-
    collection tolerance of `equiv` is already sufficient. A `None == 0`
    tolerance without a version that actually echoes it would only reduce
    diff sensitivity (AGENTS.md §9: normalize against a verified live echo
    class, not a suspicion).
    """
    changes: dict[str, tuple[Any, Any]] = {}
    for f in fields:
        want = desired.get(f)
        have = live.get(f)
        if not equiv(want, have):
            changes[f] = (want, have)
    return changes
