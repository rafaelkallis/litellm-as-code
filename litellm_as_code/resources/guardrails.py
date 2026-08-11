"""Guardrail reconciler.

Identity: `guardrail_name` (stable in the spec; the proxy keeps that column
unique and generates `guardrail_id`). Update/delete are `guardrail_id`-keyed,
so we resolve the id from the live list after matching by name.

Secrets: `litellm_params` (e.g. `api_key`) is read back MASKED, like
credentials — so it can never be diffed against live. We diff only
non-secret comparable fields (`guardrail_name`, `guardrail_info`) and
re-assert the spec's `litellm_params` whenever an update fires (write-once +
re-assert-on-change, exactly like `credential_values`).

Config-only guardrails: `/v2/guardrails/list` also returns entries loaded from
the proxy's startup `config.yaml` with `guardrail_id=None`. Those are startup
configuration, NOT DB-managed runtime state — we never diff or delete them.

Mutation: POST /guardrails | PATCH /guardrails/{id} | DELETE /guardrails/{id}.
"""

from __future__ import annotations

from typing import Any

from ..api import LiteLLMClient
from ..diff import comparable_diff
from ..types import Action, Diff

# Non-secret comparable fields. `litellm_params` is deliberately excluded:
# the API masks it on read, so diffing it would cause perpetual drift.
COMPARABLE = ["guardrail_name", "guardrail_info"]


def reconcile_guardrails(
    client: LiteLLMClient,
    spec_entries: list[dict[str, Any]],
    dry_run: bool = False,
) -> list[Diff]:
    diffs: list[Diff] = []

    # Only DB-backed guardrails are drift targets; config-file entries have no
    # guardrail_id and are startup-only (out of scope).
    live_by_name = {
        g.get("guardrail_name"): g
        for g in client.list_guardrails()
        if g.get("guardrail_name") and g.get("guardrail_id")
    }

    for entry in spec_entries:
        name: str = entry["guardrail_name"]
        existing = live_by_name.get(name)

        if existing is None:
            diffs.append(Diff("guardrail", name, Action.CREATE))
            if not dry_run:
                client.create_guardrail(entry)
            continue

        guardrail_id = existing.get("guardrail_id")
        changes = comparable_diff(entry, existing, COMPARABLE)
        diffs.append(
            Diff(
                "guardrail",
                name,
                Action.UPDATE if changes else Action.NOOP,
                changes,
            )
        )
        if changes and not dry_run:
            payload = dict(entry)
            # Re-assert litellm_params (secrets + non-secret config) since it
            # cannot be diffed against the masked read-back.
            payload.pop("guardrail_id", None)
            client.update_guardrail(guardrail_id, payload)

    return diffs
