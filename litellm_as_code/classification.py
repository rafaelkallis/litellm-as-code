"""Generic drift classification: writable / server-owned / unknown (issue #29).

The problem class: some fields the proxy *accepts* on writes but does not
*persist* — notably per-deployment model costs on LiteLLM >= 1.102 (server-
derived pricing). Diffing such a field against the live API produces a
perpetual update plan no matter how many times it is applied. Issue #29
generalizes the models-cost acceptance path into a small declarative
mechanism any reconciler stage can share.

Every (resource_type, field) is in one of three states per run:

  - **writable** (default, unknown-ish): diff normally; drift means a real
    update. Nothing special is done.
  - **server-owned with reason**: the field is accepted but not persisted;
    its drift is *classified*, not actionable. Classification NEVER skips
    the write — it only downgrades the leftover plan after applying, so the
    mechanism stays self-correcting (see below).
  - **unknown** (i.e. still the default): classification is per-run only —
    nothing persists between runs, so a proxy that *starts persisting* a
    field again is discovered by the very first verify pass of that run and
    the plan keeps the real UPDATE. Wrong classifications cannot outlive
    the run that produced them.

How a field becomes server-owned for a run:

  1. **Registry** (``VERIFIED_SERVER_OWNED``) — static classes verified
     against the pinned proxies. Deliberately EMPTY today: every currently
     known class is either version-gated (model costs, below) or normalized
     at diff time (see the ledger comment on the registry). Next class to
     land = one registry entry or one capability-gated
     ``declare_server_owned`` — never a per-resource hand-patch.
  2. **Capability facts** — a stage declares ``declare_server_owned`` for
     fields gated by a ``Capabilities`` fact (today: model cost keys when
     ``model_costs_persist is False``). Capabilities own the *gate*
     knowledge (issue #27); this module owns the *drift accounting*.
  3. **Apply-then-verify discovery** (:func:`classify_survivors`) — after
     applying updates, one re-list re-diffs every freshly-applied (desired)
     against fresh rows with the stage's own rediff closure. A field that
     still differs after WE just wrote it is demonstrated server-owned for
     the rest of the run (downgraded with ``DETECTED_REASON``).

The kernel keeps writes and accounting separate by design:

  - :func:`writable_changes` splits a change set into the writable part and
    the classified part — stages diff/apply the writable part and label the
    diff with :func:`accept_drift` for the classified part.
  - :func:`accept_drift` pops fields off a ``Diff``, downgrades it to NOOP
    when nothing writable remains, stamps
    ``SERVER_OWNED — <fields>: <reason>`` on the plan line (stdout stays
    machine-greppable — same ``Diff.__str__`` contract) and emits the
    classification notice to stderr at most once per run per distinct
    (fields, reason).
  - :func:`classify_survivors` is the generic verify pass.

Verification semantics, documented because they matter:

  - **--dry-run cannot verify** (nothing is applied, so nothing can fail to
    stick). Classified fields still show as pending updates in --dry-run on
    a proxy where the classification was *discovered* (not declared) — that
    is deliberate asymmetry, and on the live suites it is exactly what
    makes --dry-run the regression detector: a proxy that changed behavior
    shows exit 2 on dry-run where apply-only runs stay quiet. The
    capability-*declared* path (models costs) needs no verification and
    behaves identically under --dry-run and apply.
  - **Race guard**: if a row vanishes between the write and the re-list, the
    verify is impossible and the diff is left untouched — today's models
    precedent (no invented classification).
  - **Read-after-write staleness**: discovery assumes the re-list reflects
    our writes (the same assumption the pre-existing models fallback made;
    the API-backed client already retries eventual-consistency 404s).

Boundary with issue #27 (capabilities): :func:`reset_run` is called by
:meth:`reconciler.reconcile`, so notices and classifications never leak
between runs. No version-tuple comparisons live here (all of them stay
inside ``capabilities.py``, AGENTS.md §4).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .log import server_owned
from .types import Action, Diff

# ---------------------------------------------------------------------------
# Static registry of server-owned fields. Deliberately empty: quarterly
# ledger of where each known drift class is handled instead, so "not here"
# stays a documented decision rather than an omission.

VERIFIED_SERVER_OWNED: dict[tuple[str, str], str] = {}
"""Live-verified (resource_type, field) -> reason classes, unconditionally.

Known drift classes and their homes today (kept in sync with the resources
and tests that handle them):

  - model costs (``input_cost_per_token`` / ``output_cost_per_token``) on
    proxies that do not persist them: **capability-gated** — declared per
    run by ``resources.models`` from the ``model_costs_persist`` fact
    (issue #27), with an apply-then-verify fallback when the capability is
    unknown (``resources/models.py``).
  - masked secret containers / write-once re-assertion (guardrail_info,
    credential_values): **diff-time** — resources/guardrails.py +
    resources/credentials.py carry the usable-reassertion and masker-echo
    semantics (issues #11); a registry entry here would re-read secrets
    from the API, which AGENTS.md §2 forbids.
  - member role echo defaults (team/org members): **diff-time realign** —
    the default is resolved and aligned on BOTH compare sides
    (resources/members.py, issue #12), verified live (roles always echo).
  - server-injected read-only fields (spend, status, budget_reset_at,
    updated_at, ...): **not comparable** — the per-resource COMPARABLE
    lists exclude them outright; no classification needed.

Empty means: nothing is *unconditionally* server-owned across every proxy
version we support. If a field is server-owned on every reachable version
(verified against the pinned versions), seed it here with the verification
receipt in ``reason``.
"""

# Reason tag for fields classified by the generic apply-then-verify pass
# (no capability knows about them; the proxy demonstrated non-persistence
# by not persisting our write).
DETECTED_REASON = (
    "the proxy did not persist this write (classified at apply time; "
    "--dry-run cannot verify and keeps showing the update)"
)

# Per-run classification map: (resource_type, field) -> reason.
_run_reasons: dict[tuple[str, str], str] = {}
# Dedup for the stderr notices: one per run per distinct (fields, reason).
_notified: set[tuple[frozenset[str], str]] = set()


def reset_run() -> None:
    """Clear per-run classification state. Called by reconcile()."""
    _run_reasons.clear()
    _notified.clear()


def declare_server_owned(
    resource_type: str, fields: list[str] | tuple[str, ...], reason: str
) -> None:
    """Declare fields server-owned for the remainder of this run."""
    for field in fields:
        _run_reasons[(resource_type, field)] = reason


def classified_reason(resource_type: str, field: str) -> str | None:
    """The reason a field is server-owned this run, or None if writable."""
    return _run_reasons.get((resource_type, field))


def writable_changes(
    resource_type: str,
    changes: dict[str, tuple[Any, Any]],
) -> tuple[dict[str, tuple[Any, Any]], dict[str, str]]:
    """Split a change set into (writable changes, classified fields).

    Expects the per-resource ``comparable_diff`` shape:
    ``{field: (desired, live)}``. The classified part keeps its desired
    value so callers can still re-assert writes where the mechanism allows
    (idempotent re-assertions stay the resource's business; the kernel only
    reports what is *server-owned* vs *writable*).
    """
    writable: dict[str, tuple[Any, Any]] = {}
    classified: dict[str, str] = {}
    for field, change in changes.items():
        reason = classified_reason(resource_type, field)
        if reason is None:
            writable[field] = change
        else:
            classified[field] = reason
    return writable, classified


def accepted_message(classified: dict[str, str]) -> str:
    """Plan-line message for accepted drift: SERVER_OWNED — fields: reason.

    Stable marker kept greppable on stdout (Diff.__str__ contract).
    """
    fields = ", ".join(sorted(classified))
    reasons = "; ".join(sorted(set(classified.values())))
    return f"SERVER_OWNED — {fields}: {reasons}"


def _notify(classified: dict[str, str], message: str) -> None:
    key = (frozenset(classified), tuple(sorted(set(classified.values()))))
    if key in _notified:
        return
    _notified.add(key)
    server_owned(message)


def accept_drift(diff: Diff, classified: dict[str, str]) -> bool:
    """Classify server-owned fields on ``diff`` and converge it.

    ``classified`` maps field -> reason. Pops the fields off the diff's
    change set; when nothing writable remains the UPDATE becomes an
    accepted NOOP. Stamps ``SERVER_OWNED — ...`` on the plan line and
    emits the one-per-run stderr notice. Returns True when the diff
    became a NOOP.
    """
    for field in classified:
        diff.changes.pop(field, None)
    message = accepted_message(classified)
    if not diff.changes:
        diff.action = Action.NOOP
    diff.message = message
    _notify(classified, message)
    return diff.action is Action.NOOP


# The re-diff closure a stage supplies to classify_survivors. It compares
# one spec-shaped desired entry against one fresh live row and returns the
# comparable_diff shape (the stage's own drift semantics — sensitive-field
# handling, masked rewrites and per-million conversions stay in the stage).
Rediff = Callable[[dict[str, Any], dict[str, Any]], dict[str, tuple[Any, Any]]]

# Live-row index closure: rows -> {identity: row} (per-resource identity).
RowIndex = Callable[[list[dict[str, Any]]], dict[str, dict[str, Any]]]


def classify_survivors(
    resource_type: str,
    rows: list[dict[str, Any]],
    patched: list[tuple[Diff, dict[str, Any]]],
    *,
    index: RowIndex,
    rediff: Rediff,
    reason: str = DETECTED_REASON,
) -> int:
    """Generic apply-then-verify pass over freshly applied updates.

    ``patched`` is ``[(diff, desired_entry), ...]`` for the updates this
    stage just applied (write descriptors only — no raw PATCH payloads,
    which for credentials/keys would contradict the write-once posture).
    One re-list (``rows``) re-diffs each desired entry against its fresh
    row; fields that still differ are demonstrated server-owned for the
    rest of the run: declared on the run map and downgraded on the diff
    via :func:`accept_drift`.

    Race guard: a row that vanished between the write and the re-list gets
    NO classification (the verify is impossible); the diff is left exactly
    as the stage left it — the next run decides.

    Returns the number of diffs that were downgraded to NOOP.
    """
    live = index(rows)
    downgraded = 0
    for diff, desired in patched:
        row = live.get(diff.name)
        if row is None:
            continue
        survivors = sorted(set(rediff(desired, row)) & set(diff.changes))
        if not survivors:
            continue
        if accept_drift(diff, {field: reason for field in survivors}):
            downgraded += 1
    return downgraded
