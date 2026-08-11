"""Policy reconciler.

Identity: `policy_name` (stable in the spec). The policy engine persists
versions and assigns a `policy_id`; the list endpoint returns both, so we
resolve the id by name and mutate through it.

Comparable fields: `policy_name`, `inherit`, `description`, `guardrails_add`,
`guardrails_remove`. `condition` and `pipeline` are newer policy-engine
features that we intentionally do not diff (kept opaque in the spec).

Config-only policies: `/policies/list` also returns policies loaded from the
proxy's startup `config.yaml` with `definition_location="config"`. Those are
startup configuration, NOT DB-managed runtime state — we never diff or delete
them.

Mutation: POST /policies | PUT /policies/{id} | DELETE /policies/{id}.
"""

from __future__ import annotations

from typing import Any

from ..api import LiteLLMClient
from ..diff import comparable_diff
from ..types import Action, Diff

# Manageable policy fields. `condition` / `pipeline` are left out deliberately
# (newer features kept opaque in the spec).
COMPARABLE = [
    "policy_name",
    "inherit",
    "description",
    "guardrails_add",
    "guardrails_remove",
]

# The live API always echoes list-shaped fields as (possibly empty) arrays,
# even when the spec omits them. Normalize both sides so an omitted spec field
# means "empty list" instead of flagging perpetual drift.
_LIST_DEFAULTS = {"guardrails_add": [], "guardrails_remove": []}


def _normalize(d: dict[str, Any]) -> dict[str, Any]:
    d = dict(d)
    for field, default in _LIST_DEFAULTS.items():
        if d.get(field) is None:
            d[field] = default
    return d


def reconcile_policies(
    client: LiteLLMClient,
    spec_entries: list[dict[str, Any]],
    dry_run: bool = False,
) -> list[Diff]:
    diffs: list[Diff] = []

    # Only DB-backed policies are drift targets; config-file entries carry
    # definition_location="config" and are startup-only (out of scope).
    live_by_name = {
        p.get("policy_name"): p
        for p in client.list_policies()
        if p.get("policy_name")
        and p.get("definition_location", "db") == "db"
        and p.get("policy_id")
    }

    for entry in spec_entries:
        name: str = entry["policy_name"]
        existing = live_by_name.get(name)

        if existing is None:
            diffs.append(Diff("policy", name, Action.CREATE))
            if not dry_run:
                client.create_policy(entry)
            continue

        policy_id = existing.get("policy_id")
        changes = comparable_diff(_normalize(entry), _normalize(existing), COMPARABLE)
        diffs.append(
            Diff("policy", name, Action.UPDATE if changes else Action.NOOP, changes)
        )
        if changes and not dry_run:
            payload = dict(entry)
            payload.pop("policy_id", None)
            client.update_policy(policy_id, payload)

    return diffs
