"""User reconciler.

Identity: `user_id` (stable UUID in the spec).
Comparable fields: user_alias, user_email, user_role (plus budget/limits).
Mutation: POST /user/new  | POST /user/update.
"""

from __future__ import annotations

from typing import Any

from ..api import LiteLLMClient
from ..diff import comparable_diff
from ..types import Action, Diff

# Fields we can compare & manage. LiteLLM injects server defaults for many
# others; keep this list tight to avoid perpetual drift.
COMPARABLE = ["user_alias", "user_email", "user_role", "auto_create_key"]


def reconcile_users(
    client: LiteLLMClient,
    spec_entries: list[dict[str, Any]],
    dry_run: bool = False,
) -> list[Diff]:
    diffs: list[Diff] = []
    live = {u["user_id"]: u for u in client.list_users()}

    for entry in spec_entries:
        user_id = entry["user_id"]
        alias = entry.get("user_alias", user_id)
        existing = live.get(user_id)

        if existing is None:
            diffs.append(Diff("user", alias, Action.CREATE))
            if not dry_run:
                client.create_user(entry)
            continue

        changes = comparable_diff(entry, existing, COMPARABLE)
        diffs.append(
            Diff("user", alias, Action.UPDATE if changes else Action.NOOP, changes)
        )
        if changes and not dry_run:
            client.update_user(entry)

    return diffs
