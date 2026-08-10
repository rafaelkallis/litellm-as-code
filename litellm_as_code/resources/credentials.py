"""Credential reconciler.

Identity: `credential_name`.
Comparable fields: credential_info (non-secret). `credential_values` are
never returned by the API, so they are diffed against `.state.json` (sent
only on create or when the local state differs from the spec).

Mutation: POST /credentials | PATCH /credentials/{name} | DELETE /credentials/{name}.
"""

from __future__ import annotations

from typing import Any

from ..api import LiteLLMClient
from ..diff import comparable_diff
from ..state import State
from ..types import Action, Diff

COMPARABLE = ["credential_info", "model_id"]


def reconcile_credentials(
    client: LiteLLMClient,
    spec_entries: list[dict[str, Any]],
    state: State,
    dry_run: bool = False,
) -> list[Diff]:
    diffs: list[Diff] = []
    live = {c["credential_name"]: c for c in client.list_credentials()}

    for entry in spec_entries:
        name = entry["credential_name"]
        existing = live.get(name)
        desired_values = entry.get("credential_values", {})
        applied_values = state.credentials.get(name, {})

        if existing is None:
            diffs.append(Diff("credential", name, Action.CREATE))
            if not dry_run:
                client.create_credential(entry)
                state.credentials[name] = dict(desired_values)
            continue

        # comparable, non-secret drift against live
        changes = dict(comparable_diff(entry, existing, COMPARABLE))

        # secret drift against local applied-state (API never echoes values)
        if desired_values != applied_values:
            changes["credential_values"] = (applied_values, desired_values)

        diffs.append(
            Diff("credential", name, Action.UPDATE if changes else Action.NOOP, changes)
        )
        if changes and not dry_run:
            client.patch_credential(name, entry)
            state.credentials[name] = dict(desired_values)

    return diffs
