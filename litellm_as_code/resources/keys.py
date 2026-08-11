"""Virtual API key reconciler.

Identity: `key_alias` (stable in the spec; the API enforces uniqueness), so
key existence is decided purely from `/key/list` — no local state needed.

Comparable (non-secret) fields: key_type->(managed fields), user_id, models,
budget, allowed_routes. The raw `key` value is write-once:
- on CREATE we send the spec's desired `key` if present (LiteLLM accepts a
  caller-supplied `key` on `/key/generate`); otherwise the proxy mints one.
- on UPDATE we send only non-secret fields and never include `key`, so an
  existing key is never re-asserted (which would count as rotation).

Mutation: POST /key/generate | POST /key/update | POST /key/delete.
"""

from __future__ import annotations

from typing import Any

from ..api import LiteLLMClient
from ..diff import comparable_diff
from ..types import Action, Diff

# Non-secret, comparable key attributes.
COMPARABLE = ["user_id", "team_id", "models", "max_budget", "budget_duration", "allowed_routes"]


def reconcile_keys(
    client: LiteLLMClient,
    spec_entries: list[dict[str, Any]],
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
                # Send the desired key once; LiteLLM mints one if absent.
                client.generate_key(entry)
            continue

        # Only update non-secret fields. `key` is deliberately never sent on
        # update, so existing keys are never rotated by this reconciler.
        changes = comparable_diff(entry, existing, COMPARABLE)
        diffs.append(
            Diff("key", alias, Action.UPDATE if changes else Action.NOOP, changes)
        )
        if changes and not dry_run:
            payload = dict(entry)
            payload.pop("key", None)  # don't re-assert / rotate
            client.update_key(payload)

    return diffs
