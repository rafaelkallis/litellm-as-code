"""Credential reconciler.

Identity: `credential_name` (unique at the DB level). Existence is decided
purely from `/credentials` — no local state needed.

Comparable (non-secret) fields: credential_info, model_id. The API never
returns `credential_values` in plaintext (it masks them), so they cannot be
diffed against live. They are therefore write-once:
- on CREATE we send the spec's `credential_values`;
- on UPDATE only comparable non-secret fields are diffed; when anything
  changes we re-assert the full payload (values included). PATCH is
  idempotent and does not rotate — it is safe to re-send on demand.

Mutation: POST /credentials | PATCH /credentials/{name} | DELETE /credentials/{name}.
"""

from __future__ import annotations

from typing import Any

from ..api import LiteLLMClient
from ..diff import comparable_diff
from ..types import Action, Diff

COMPARABLE = ["credential_info", "model_id"]


def reconcile_credentials(
    client: LiteLLMClient,
    spec_entries: list[dict[str, Any]],
    dry_run: bool = False,
) -> list[Diff]:
    diffs: list[Diff] = []
    live = {c["credential_name"]: c for c in client.list_credentials()}

    for entry in spec_entries:
        name = entry["credential_name"]
        existing = live.get(name)

        if existing is None:
            diffs.append(Diff("credential", name, Action.CREATE))
            if not dry_run:
                client.create_credential(entry)
            continue

        # comparable, non-secret drift against live
        changes = dict(comparable_diff(entry, existing, COMPARABLE))

        diffs.append(
            Diff("credential", name, Action.UPDATE if changes else Action.NOOP, changes)
        )
        if changes and not dry_run:
            # Re-assert the full payload (values included). PATCH is idempotent
            # and does not rotate, and the API never echoes values, so there is
            # nothing to diff against for secrets.
            client.patch_credential(name, entry)

    return diffs
