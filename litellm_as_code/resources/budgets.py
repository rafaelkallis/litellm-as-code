"""Budget reconciler.

Identity: `budget_id` (assertable on create; LiteLLM generates one if omitted).

Comparable fields: the manageable budget limits table (`max_budget`,
`soft_budget`, `max_parallel_requests`, `tpm_limit`, `rpm_limit`,
`model_max_budget`, `budget_duration`). We deliberately diff only these —
LiteLLM reads back `budget_reset_at` (server-computed from the duration),
`spend` and timestamps, which would cause perpetual drift if adopted into
desired state.

Mutation: POST /budget/new | POST /budget/update | POST /budget/delete.
"""

from __future__ import annotations

from typing import Any

from ..api import LiteLLMClient
from ..diff import comparable_diff
from ..types import Action, Diff

# Manageable budget-table fields. `budget_reset_at` is server-injected runtime
# state (recomputed on every update from budget_duration) and is deliberately
# not comparable.
COMPARABLE = [
    "max_budget",
    "soft_budget",
    "max_parallel_requests",
    "tpm_limit",
    "rpm_limit",
    "model_max_budget",
    "budget_duration",
]


def reconcile_budgets(
    client: LiteLLMClient,
    spec_entries: list[dict[str, Any]],
    dry_run: bool = False,
) -> list[Diff]:
    diffs: list[Diff] = []
    live = {b.get("budget_id"): b for b in client.list_budgets() if b.get("budget_id")}

    for entry in spec_entries:
        budget_id = entry["budget_id"]
        display = budget_id or entry.get("budget_duration", "(unnamed)")
        existing = live.get(budget_id)

        if existing is None:
            diffs.append(Diff("budget", display, Action.CREATE))
            if not dry_run:
                client.create_budget(entry)
            continue

        changes = comparable_diff(entry, existing, COMPARABLE)
        diffs.append(
            Diff("budget", display, Action.UPDATE if changes else Action.NOOP, changes)
        )
        if changes and not dry_run:
            client.update_budget(entry)

    return diffs
