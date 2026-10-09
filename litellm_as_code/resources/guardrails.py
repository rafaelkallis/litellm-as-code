"""Guardrail reconciler.

Identity: `guardrail_name` (stable in the spec; the proxy keeps that column
unique and generates `guardrail_id`). Update/delete are `guardrail_id`-keyed,
so we resolve the id from the live list after matching by name.

Secrets: `litellm_params` (e.g. `api_key`) is read back MASKED, like
credentials — so it can never be diffed against live. `guardrail_info` is
diffed per key but ONLY over its non-secret subset (sensitive key names and
masker-shaped values carry no drift signal; see secrets.py). We also re-assert
the spec's `litellm_params` whenever an update fires (write-once +
re-assert-on-change, exactly like `credential_values`).

`guardrail_info` updates replace the whole nested map in-flight, so an update
whose spec map omits a live secret entry would DELETE that write-once value
(the plaintext cannot be read back for re-assertion). Such updates are
DEFERRED — the diff keeps surfacing the change with a message that tells the
operator how to unblock (declare the entry unmasked in the spec), but nothing
is patched until then.

Config-only guardrails: `/v2/guardrails/list` also returns entries loaded from
the proxy's startup `config.yaml` with `guardrail_id=None`. Those are startup
configuration, NOT DB-managed runtime state — we never diff or delete them.

Mutation: POST /guardrails | PATCH /guardrails/{id} | DELETE /guardrails/{id}.
"""

from __future__ import annotations

from typing import Any

from ..api import LiteLLMClient
from ..diff import comparable_diff
from ..secrets import is_masked_value, is_secret_entry
from ..types import Action, Diff, ReconcilerError

# Non-secret comparable fields. `litellm_params` is deliberately excluded:
# the API masks it on read, so diffing it would cause perpetual drift.
# `guardrail_info` IS comparable, but only its NON-SECRET subset — masked
# entries in the live echo are skipped (see _guardrail_info_changes).
COMPARABLE = ["guardrail_name"]


class _Absent:
    """Marker for a key that is absent from one side of the map.

    `guardrail_info` is a replacement map, so `"k" in <map>` and
    `<map>["k"] = None` are distinct desired states; `.get()` would conflate
    them and hide the drift (Copilot r4 on PR #20). The marker keeps the
    changes tuples renderable (see __repr__).
    """

    def __repr__(self) -> str:
        return "absent"


_MISSING = _Absent()


def _guardrail_info_changes(want: Any, have: Any) -> dict[str, tuple[Any, Any]]:
    """Per-key diff of a dict `guardrail_info`, skipping secret-shaped keys.

    Secret entries (sensitive key names, values in the masker's output
    shape — see secrets.py) carry NO drift signal: the exporter strips them
    at export time (they are write-once), so a spec's benign subset must not
    churn against a live dict still holding the masked entry (issue #11,
    PR #20 Copilot r1). Non-dict payloads stay opaque and keep the exact
    equality `comparable_diff` would apply, with one exception: an absent
    spec key against a dict live value diffs per-key over the live vector,
    so a fully-secret-stripped export is not a hard None-vs-dict conflict.
    Key MEMBERSHIP is compared alongside values: an absent key and a key
    explicitly set to `None` are distinct states of a replacement map, and
    the missing side is rendered via the `_MISSING` marker (Copilot r4 on
    PR #20).
    """
    if isinstance(have, dict):
        want_keys = want if isinstance(want, dict) else {}
        changes: dict[str, tuple[Any, Any]] = {}
        for k in sorted(set(want_keys) | set(have)):
            in_want, in_have = k in want_keys, k in have
            if in_want and is_secret_entry(k, want_keys[k]):
                continue
            if in_have and is_secret_entry(k, have[k]):
                continue
            if in_want and in_have:
                if want_keys[k] != have[k]:
                    changes[f"guardrail_info.{k}"] = (want_keys[k], have[k])
            elif in_want:  # spec declares, live lacks: (re)addition drift
                changes[f"guardrail_info.{k}"] = (want_keys[k], _MISSING)
            else:  # live has, spec omits: removal drift
                changes[f"guardrail_info.{k}"] = (_MISSING, have[k])
        return changes
    # not a dict on the live side: comparable_diff semantics (exact value)
    if want == have:
        return {}
    return {"guardrail_info": (want, have)}


def _dropped_secret_keys(entry: Any, existing: dict[str, Any]) -> list[str]:
    """Live secret entries an update payload's `guardrail_info` would delete.

    The proxy replaces the nested map on PATCH, so any live secret entry
    (sensitive key name or masker-shaped value — see secrets.py) that the
    spec's map does not declare would be silently destroyed, and its
    plaintext cannot be read back for re-assertion (issue #11, PR #20
    Copilot r2). Such entries block the update.
    """
    want_info = entry.get("guardrail_info")
    if not isinstance(want_info, dict):
        want_info = {}
    have_info = existing.get("guardrail_info")
    if not isinstance(have_info, dict):
        return []
    return sorted(
        k
        for k in have_info
        if is_secret_entry(k, have_info[k])
        and (k not in want_info or is_masked_value(want_info[k]))
        # a masked-shaped spec value is not a usable re-assertion either
    )


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
        changes.update(
            _guardrail_info_changes(
                entry.get("guardrail_info"), existing.get("guardrail_info")
            )
        )
        if not changes:
            diffs.append(Diff("guardrail", name, Action.NOOP))
            continue

        at_risk = _dropped_secret_keys(entry, existing)
        if at_risk:
            # Writing the change would replace the live map and destroy
            # write-once secrets the spec doesn't (and can't) re-declare:
            # defer. Plan-only runs surface it as a diff; apply REFUSES
            # (nonzero exit) — returning success while drift remains
            # indefinitely would lie to automation (Copilot r2 + r4 on
            # PR #20).
            reason = (
                "update would drop write-once guardrail_info secret(s) "
                f"{', '.join(at_risk)} whose plaintext cannot be "
                "re-asserted — declare them (unmasked) in the spec to apply"
            )
            diffs.append(
                Diff(
                    "guardrail",
                    name,
                    Action.UPDATE,
                    changes,
                    message=f"deferred: {reason}",
                )
            )
            if not dry_run:
                raise ReconcilerError(f"guardrail '{name}': {reason}")
            continue

        diffs.append(Diff("guardrail", name, Action.UPDATE, changes))
        if dry_run:
            continue
        payload = dict(entry)
        # Re-assert litellm_params (secrets + non-secret config) since it
        # cannot be diffed against the masked read-back.
        payload.pop("guardrail_id", None)
        if "guardrail_info" not in payload and any(
            k.startswith("guardrail_info") for k in changes
        ):
            # The spec omits guardrail_info but drift was detected in the
            # live map (e.g. benign entries it no longer declares): omitting
            # the field from the PATCH would leave the live map uncleaned and
            # re-drift forever. Materialize the desired (empty) map (Copilot
            # r5 on PR #20).
            payload["guardrail_info"] = {}
        client.update_guardrail(guardrail_id, payload)

    return diffs
