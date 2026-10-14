"""Budget reconciler.

Identity: `budget_id` (required in the spec; LiteLLM-generated ids assigned at
create are not reconcilable, so an entry must always carry a known id).

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
from ..classification import DETECTED_REASON, classify_survivors
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
    # (Diff, desired) pairs freshly applied THIS run; the apply-then-verify
    # pass below re-diffs them against one fresh re-list (issue #29).
    patched: list[tuple[Diff, dict[str, Any]]] = []

    for entry in spec_entries:
        budget_id = entry["budget_id"]
        display = budget_id
        existing = live.get(budget_id)

        if existing is None:
            diffs.append(Diff("budget", display, Action.CREATE))
            if not dry_run:
                client.create_budget(entry)
            continue

        changes = comparable_diff(entry, existing, COMPARABLE)
        diff = Diff("budget", display, Action.UPDATE if changes else Action.NOOP, changes)
        diffs.append(diff)
        if changes and not dry_run:
            client.update_budget(entry)
            patched.append((diff, entry))

    # ==== Apply-then-verify classification (non-dry-run only, issue #29) ====
    # A field that still differs after OUR update_budget just wrote it is
    # demonstrated server-owned for this run: accepted on the diff (UPDATE
    # downgrades to a NOOP plan line carrying the SERVER_OWNED message; the
    # stderr notice fires at most once per run). Verification is
    # self-correcting per run — if the proxy starts persisting the field
    # again, the next run's verify sees it stick and the plan keeps the real
    # UPDATE. --dry-run cannot verify (nothing is applied), so it keeps
    # showing such drift as a pending update — the regression detector.
    # Race guard: a budget row that vanished between the write and the
    # re-list gets NO classification (the Diff stays untouched — the next
    # run decides).
    if not dry_run and patched:
        classify_survivors(
            "budget",
            client.list_budgets(),
            patched,
            index=lambda rows: {
                b.get("budget_id"): b for b in rows if b.get("budget_id")
            },
            rediff=lambda entry, row: comparable_diff(entry, row, COMPARABLE),
            reason=DETECTED_REASON,
        )

    return diffs
