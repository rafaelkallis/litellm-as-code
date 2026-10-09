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

`guardrail_info` updates replace the whole nested map in-flight, and
`litellm_params` is shallow-merged (re-declared containers replace their live
counterparts), so an update the spec cannot match to the live secrets inside
those containers would DELETE write-once values (the plaintext cannot be read
back for re-assertion). Such updates are DEFERRED — the diff keeps surfacing
the change with a message that tells the operator how to unblock (declare the
entry unmasked in the spec), the apply refuses with a non-zero exit, and
nothing is patched until then (Copilot r2/r4/r12 on PR #20).

Config-only guardrails: `/v2/guardrails/list` also returns entries loaded from
the proxy's startup `config.yaml` with `guardrail_id=None`. Those are startup
configuration, NOT DB-managed runtime state — we never diff or delete them.

Mutation: POST /guardrails | PATCH /guardrails/{id} | DELETE /guardrails/{id}.
"""

from __future__ import annotations

from typing import Any

from ..api import LiteLLMClient
from ..diff import comparable_diff
from ..secrets import (
    MASK_PLACEHOLDER,
    is_masked_value,
    is_secret_entry,
    scrub_value,
)
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
                # Compare the secrets.py recursive SCRUB of both values: a
                # nested secret (e.g. headers.Authorization) carries no drift
                # signal, so a spec secretly-stripped one level deep must not
                # churn against the live echo holding it (Copilot r9 on PR
                # #20).
                w_clean, _ = scrub_value(want_keys[k])
                h_clean, _ = scrub_value(have[k])
                if w_clean != h_clean:
                    changes[f"guardrail_info.{k}"] = (w_clean, h_clean)
            elif in_want:  # spec declares, live lacks: (re)addition drift
                # scrub even the one-sided value: nested secrets must never
                # reach Diff.changes and be rendered into dry-run/log output
                # (Copilot r11 on PR #20)
                w_clean, _ = scrub_value(want_keys[k])
                changes[f"guardrail_info.{k}"] = (w_clean, _MISSING)
            else:  # live has, spec omits: removal drift
                h_clean, _ = scrub_value(have[k])
                changes[f"guardrail_info.{k}"] = (_MISSING, h_clean)
        return changes
    # not a dict on the live side: comparable_diff semantics (exact value),
    # but with BOTH sides scrubbed first — a desired dict can still carry
    # write-once material, and the raw tuple would render it into plan
    # output (Copilot r13 on PR #20)
    w_clean, _ = scrub_value(want)
    h_clean, _ = scrub_value(have)
    if w_clean == h_clean:
        return {}
    return {"guardrail_info": (w_clean, h_clean)}


def _secret_paths(value: Any, prefix: str = "") -> list[str]:
    """Dotted paths of secret-shaped entries inside any container (r9+r10).

    Sensitive-keyed non-None entries and masker-shaped values anywhere in
    the tree — lists included (e.g. guardrail_info.headers.Authorization or
    rules[0].Authorization) — the write-once material a replacement-PATCH
    payload could erase.
    """
    paths: list[str] = []
    if isinstance(value, dict):
        for k, v in value.items():
            path = f"{prefix}.{k}" if prefix else k
            if is_secret_entry(k, v):
                paths.append(path)
            elif isinstance(v, (dict, list)):
                paths.extend(_secret_paths(v, path))
    elif isinstance(value, list):
        for i, item in enumerate(value):
            if isinstance(item, (dict, list)):
                paths.extend(_secret_paths(item, f"{prefix}[{i}]"))
            elif is_masked_value(item):
                paths.append(f"{prefix}[{i}]")
    return paths


def _dropped_secret_paths(
    want: Any,
    have: dict[str, Any] | list[Any],
    prefix: str = "",
) -> list[str]:
    """Nested secret entries an update payload's `guardrail_info` would
    delete, dotted-path keyed (dicts AND list elements — Copilot r10).

    The proxy replaces the whole nested map on PATCH, so any live secret
    entry (sensitive key name or masker-shaped value — see secrets.py) that
    the spec does not re-declare with usable material (non-null, non-masked)
    would be silently destroyed, and its plaintext cannot be read back for
    re-assertion (issue #11, PR #20 Copilot r2 + r9). Such paths block the
    update.
    """
    out: list[str] = []
    if isinstance(have, dict):
        items: list[tuple[Any, Any]] = list(have.items())
    else:  # list elements: match by index
        items = list(enumerate(have))
    for k, v in items:
        path = (
            f"{prefix}.{k}" if isinstance(have, dict) and prefix else (
                str(k) if isinstance(have, dict) else f"{prefix}[{k}]"
            )
        )
        w = _declared(want, k)
        if is_secret_entry(str(k), v) and not _usable_reassertion(w):
            # the spec is absent/null/masked here — a null or masker-shaped
            # spec value is not a usable re-assertion either: it would write
            # "no secret" (or the mask) over the live write-once value
            out.append(path)
        elif isinstance(v, (dict, list)):
            if isinstance(w, type(v)):
                out.extend(_dropped_secret_paths(w, v, path))
            else:  # the spec declares nothing (or a different container)
                # here: the whole risky subtree goes with the replacement
                out.extend(_secret_paths(v, path))
    return out


def _declared(want: Any, key: Any) -> Any:
    """The spec's declared value at `key` (mapping key or list index)."""
    if isinstance(want, dict):
        return want.get(key)
    if isinstance(want, list) and isinstance(key, int) and key < len(want):
        return want[key]
    return None


def _usable_reassertion(declared: Any) -> bool:
    """True when the spec's declared value is usable desired state for a
    live secret node: present, non-null and neither a masker echo nor the
    export placeholder (null/masked/placeholder would write "no secret",
    the mask, or nothing over the write-once value)."""
    return (
        declared is not None
        and not is_masked_value(declared)
        and declared != MASK_PLACEHOLDER
    )


def _dropped_secret_keys(entry: Any, existing: dict[str, Any]) -> list[str]:
    """Write-once secret paths at risk in this guardrail's update payload."""
    want_info = entry.get("guardrail_info")
    have_info = existing.get("guardrail_info")
    if not isinstance(have_info, dict):
        return []
    return sorted(
        _dropped_secret_paths(
            want_info if isinstance(want_info, dict) else {}, have_info
        )
    )


def _dropped_secret_params(entry: Any, existing: dict[str, Any]) -> list[str]:
    """Write-once paths inside `litellm_params` a PATCH could erase (r12).

    LiteLLM v1.97.0 shallow-merges `litellm_params`: top-level keys the spec
    omits are preserved by the proxy, but a container the spec DOES declare
    (e.g. the scrubbed export's `headers: {X-Foo: bar}`) replaces the whole
    live nested map — erasing any stripped write-once entry inside it. Only
    differences within re-declared containers are therefore at risk.
    """
    want_params = entry.get("litellm_params")
    have_params = existing.get("litellm_params")
    if not isinstance(want_params, dict) or not isinstance(have_params, dict):
        return []
    out: list[str] = []
    for k, v in want_params.items():
        if not isinstance(v, (dict, list)):
            continue  # scalar the spec re-asserts: desired state, never at risk
        h = have_params.get(k)
        if not isinstance(h, type(v)):
            continue  # live container absent: nothing write-once to erase
        out.extend(_dropped_secret_paths(v, h, k))
    return out


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

        at_risk = sorted(
            set(_dropped_secret_keys(entry, existing))
            | set(_dropped_secret_params(entry, existing))
        )
        if at_risk:
            # Writing the change would replace/erase write-once secrets the
            # spec doesn't (and can't) re-declare: defer. Plan-only runs
            # surface it as a diff; apply REFUSES (nonzero exit) — returning
            # success while drift remains indefinitely would lie to
            # automation (Copilot r2 + r4 + r12 on PR #20).
            reason = (
                "update would drop write-once secret(s) "
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
