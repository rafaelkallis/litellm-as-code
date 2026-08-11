"""Virtual API key reconciler.

Identity: `key_alias` (stable in the spec).
Comparable (non-secret) fields: key_type->(managed fields), user_id, models,
budget, allowed_routes. The raw `key` value is NOT compared against the API;
it is diffed against `.state.json` so we never rotate an existing key.

The live API never returns the raw key value, so:
- on CREATE we record the (optional) desired key into state;
- on UPDATE we only send non-secret fields and never include `key`,
  preventing accidental rotation.

Mutation: POST /key/generate | POST /key/update (no `key` field) | POST /key/delete.
"""

from __future__ import annotations

from typing import Any

from ..api import LiteLLMClient
from ..diff import comparable_diff
from ..state import State
from ..types import Action, Diff

# Non-secret, comparable key attributes.
COMPARABLE = ["user_id", "team_id", "models", "max_budget", "budget_duration", "allowed_routes"]


def reconcile_keys(
    client: LiteLLMClient,
    spec_entries: list[dict[str, Any]],
    state: State,
    dry_run: bool = False,
) -> list[Diff]:
    diffs: list[Diff] = []
    live = {k["key_alias"]: k for k in client.list_keys() if k.get("key_alias")}

    for entry in spec_entries:
        alias = entry["key_alias"]
        existing = live.get(alias)

        if existing is None:
            diffs.append(Diff("key", alias, Action.CREATE))
            if not dry_run:
                generated = client.generate_key(entry)
                # Persist what we know about this key so future runs don't recreate.
                state.keys[alias] = {"key": entry.get("key", generated.get("key"))}
            continue

        # Only update non-secret fields. `key` is deliberately never sent on
        # update, so existing keys are never rotated by this reconciler.
        changes = comparable_diff(entry, existing, COMPARABLE)
        diffs.append(
            Diff("key", alias, Action.UPDATE if changes else Action.NOOP, changes)
        )
        if changes and not dry_run:
            payload = dict(entry)
            payload.pop("key", None)  # don't rotate
            client.update_key(payload)
            # The API never echoes the raw key, so only fill in a blank entry if
            # one is somehow missing from state (should not happen after a create).
            state.keys.setdefault(alias, {"key": existing.get("key", "")})

    return diffs
