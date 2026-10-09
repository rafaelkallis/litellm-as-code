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

Versioning: policies are versioned. `PUT /policies/{id}` only accepts DRAFT
versions — published/production rows reject updates ("Only draft versions can
be updated"). Drift is therefore reconciled PUT-first: a draft policy is
updated in place; a publish-locked policy falls back to delete + re-create
under the same `policy_name` (which creates a new production version). NOTE
the fallback is NOT atomic: if the re-create fails the policy is left
destroyed (worse than drift). That residual risk is accepted deliberately
(issue #10) — it surfaces loudly through the CLI error path rather than
silently.

Mutation: POST /policies | PUT /policies/{id} (drafts) | DELETE /policies/{id}.
"""

from __future__ import annotations

from typing import Any

from ..api import LiteLLMClient
from ..diff import comparable_diff
from ..types import Action, Diff, ReconcilerError

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
        if not changes:
            diffs.append(Diff("policy", name, Action.NOOP))
            continue

        # Prefer the in-place PUT: draft versions accept it and it does not
        # burn a version-history entry. Published/production policies reject
        # PUT with the proxy's draft-only error; only then fall back to
        # recreate (delete + re-create; see module docstring for the accepted
        # non-atomicity) — issue #10.
        diff = Diff("policy", name, Action.UPDATE, changes)
        diffs.append(diff)
        if not dry_run:
            try:
                client.update_policy(policy_id, entry)
                diff.message = "updated in place (draft PUT)"
            except ReconcilerError as e:
                if "Only draft versions" not in str(e):
                    raise
                diff.message = "recreate (draft-only PUT rejected)"
                client.delete_policy(policy_id)
                client.create_policy(entry)
        else:
            # Dry-run cannot probe the PUT without mutating a draft; report
            # both outcomes the apply could take.
            diff.message = "update (draft PUT) or recreate (production)"

    return diffs
