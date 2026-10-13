"""Model reconciler.

Identity: `model_name` (e.g. "logarithmus/large").
Comparable fields: the managed subset of model_info/litellm_params.
Mutation: POST /model/new | PATCH /model/{id}/update | POST /model/delete.

Cost conversion: the spec may express `input_cost_per_million_tokens` /
`output_cost_per_million_tokens` (ecosystem convention); the reconciler
converts to per-token when sending to the API, and back when comparing.

Cost acceptance (LiteLLM >= 1.102): since v1.102.0 the proxy does not persist
per-deployment costs, so a cost-only drift can never converge by patching.
The decision flows from litellm_as_code.capabilities (the single home of every
capability fact, including this cutoff hint): when the hint announces a
>= 1.102 generation the cost keys are dropped from the change set and the
drift is accepted with a message, so BOTH apply and --dry-run converge
without patching. When the hint is unavailable (OpenAPI probe disabled) the
fallback is an apply-then-verify acceptance on non-dry-run runs — the ONE
place that fallback exists (this acceptance phase); --dry-run cannot verify
(it patches nothing), so in THAT fallback a dry-run keeps showing the cost
update.
"""

from __future__ import annotations

from typing import Any

from ..api import LiteLLMClient
from ..capabilities import capabilities
from ..log import warn
from ..types import Action, Diff


def _cost_to_per_token(v: Any) -> float | None:
    """Convert a per-million value to per-token, or passthrough."""
    if v is None:
        return None
    return float(v) / 1_000_000.0


# The only per-token cost keys the reconciler manages.
_COST_KEYS = ("input_cost_per_token", "output_cost_per_token")

# SERVER_DERIVED_PRICING — distinguishing marker for cost acceptance.
# Pinning the facts (audited, live-verified 2026-10):
# - Since LiteLLM v1.102.0 (upstream 76cb0fec1 "stop persisting cost map
#   pricing as a deployment override" + 42541a923/5d3d99eb9 "drop echoed
#   cost-map pricing"), per-deployment costs (input_cost_per_token /
#   output_cost_per_token) sent to POST /model/new and
#   PATCH /model/{id}/update are NOT persisted; on >= 1.102 /model/info
#   reads them back as null even immediately after create/patch.
# - On the pinned v1.97.0 generation (our integration image) costs DO
#   round-trip.
# - The version hint for this cutoff lives in litellm_as_code.capabilities
#   (_COSTS_PERSIST_BELOW) — like every capability fact, in one place only.
# Effect otherwise: perpetual `model X would be updated (input_cost_per_token:
# 2e-06 -> None, ...)` — the plan never converges, --dry-run wedges at exit
# code 2 forever, and apply sends a dead PATCH every run (harmless, kept).
# Pricing on such proxies is server-derived from the proxy's cost map.
_ACCEPTED_PRICING_MESSAGE = (
    "SERVER_DERIVED_PRICING — cost-only drift accepted: this proxy (LiteLLM "
    ">= 1.102) does not persist per-deployment costs; pricing is "
    "server-derived from the proxy's cost map"
)


def _resolve_model_id(remote: dict[str, Any]) -> str | None:
    return (remote.get("model_info") or {}).get("id")


def _comparable_model(remote: dict[str, Any]) -> dict[str, Any]:
    mi = remote.get("model_info") or {}
    lp = remote.get("litellm_params") or {}
    out: dict[str, Any] = {}
    for k in ("mode", "base_model", "tier"):
        if mi.get(k) is not None:
            out[k] = mi[k]
    for k in ("custom_llm_provider", "model", "litellm_credential_name"):
        if lp.get(k) is not None:
            out[k] = lp[k]
    for k in ("input_cost_per_token", "output_cost_per_token"):
        # The proxy echoes per-token costs under model_info; some versions and
        # our fake store them under litellm_params. Read whichever carries it.
        v = mi.get(k)
        if v is None:
            v = lp.get(k)
        if v is not None:
            out[k] = v

    # With STORE_MODEL_IN_DB the proxy echoes the *per-million* costs back
    # under model_info (the DB columns) and recomputes per-token on the fly
    # (0 when the cost table isn't loaded). Carry those over so a non-zero
    # per-million value that maps to per-token can be compared in either unit.
    for k in ("input_cost_per_million_tokens", "output_cost_per_million_tokens"):
        if mi.get(k) is not None:
            out[k] = mi[k]
    return out


def _cost_equal(want: Any, have: Any) -> bool:
    """Compare costs tolerantly: 0 == 0.0, numeric equality across types.

    The proxy stores zero costs as int `0` and non-zero per-token values as
    float; our per-token conversion always yields float. A literal `!=` would
    flag `0 != 0.0` as perpetual drift, so compare numerically when both sides
    are numbers.
    """
    if want == have:
        return True
    if isinstance(want, (int, float)) and isinstance(have, (int, float)):
        return float(want) == float(have)
    return False


def _per_token_equal(
    want: Any, have: dict[str, Any], per_million_key: str
) -> bool:
    """True when a desired per-token cost matches what the live model reports.

    The proxy can report the same cost in two units: the per-token value it
    recomputes (often 0 for DB-backed models whose cost table isn't loaded)
    or the authoritative per-million columns. Accept a match in either unit:

    - exact per-token match (numeric-tolerant), or
    - desired per-token == live per-million / 1e6, or
    - desired per-token == 0 and live per-million == 0.0 (both "no cost").
    """
    have_per_token = have.get("input_cost_per_token" if per_million_key.startswith("input") else "output_cost_per_token")
    if _cost_equal(want, have_per_token):
        return True
    live_per_million = have.get(per_million_key)
    if live_per_million is not None and isinstance(live_per_million, (int, float)):
        if _cost_equal(want, float(live_per_million) / 1_000_000.0):
            return True
    return False


def _desired_cost_on_live(want_key: str, want_value: Any, have: dict[str, Any]) -> bool:
    """True when a desired per-token cost is present on the live model."""
    if want_key == "input_cost_per_token":
        return _per_token_equal(want_value, have, "input_cost_per_million_tokens")
    if want_key == "output_cost_per_token":
        return _per_token_equal(want_value, have, "output_cost_per_million_tokens")
    return want_value == have.get(want_key)


def _desired_model_state(entry: dict[str, Any]) -> dict[str, Any]:
    """The spec entry's managed state, costs converted to per-token units.

    None-valued fields are dropped so an unset spec field never reads as
    drift against the live echo.
    """
    mi = entry.get("model_info") or {}
    lp = entry.get("litellm_params") or {}
    # per-million -> per-token for the compare (spec convention, AGENTS.md §5)
    want = {
        "mode": mi.get("mode"),
        "base_model": mi.get("base_model"),
        "tier": mi.get("tier"),
        "custom_llm_provider": lp.get("custom_llm_provider"),
        "model": lp.get("model"),
        "litellm_credential_name": lp.get("litellm_credential_name"),
        "input_cost_per_token": (
            _cost_to_per_token(mi.get("input_cost_per_million_tokens"))
            if mi.get("input_cost_per_million_tokens") is not None
            else mi.get("input_cost_per_token")
        ),
        "output_cost_per_token": (
            _cost_to_per_token(mi.get("output_cost_per_million_tokens"))
            if mi.get("output_cost_per_million_tokens") is not None
            else mi.get("output_cost_per_token")
        ),
    }
    return {k: v for k, v in want.items() if v is not None}


def _model_changes(
    want: dict[str, Any], have: dict[str, Any]
) -> dict[str, tuple[Any, Any]]:
    """Comparable drift between desired and the live echo.

    Costs compare with unit tolerance (per-token vs the per-million/1e6
    echo, 0 == 0.0, int vs float); `model` compares the remote
    "<provider>/<base>" echo against litellm_params.model literally.
    """
    changes: dict[str, tuple[Any, Any]] = {}
    for k, w in want.items():
        h = have.get(k)
        if k == "input_cost_per_token":
            equal = _per_token_equal(w, have, "input_cost_per_million_tokens")
        elif k == "output_cost_per_token":
            equal = _per_token_equal(w, have, "output_cost_per_million_tokens")
        else:
            equal = w == h
        if not equal:
            changes[k] = (w, h)
    return changes


def _suppress_cost_drift(
    changes: dict[str, tuple[Any, Any]], costs_persist: bool | None
) -> bool:
    """Drop unmanageable per-token cost keys from the change set (>= 1.102).

    Called when the version probe already told us the generation: the plan
    then only describes deltas that can actually be applied. Returns True
    when anything was dropped.
    """
    if costs_persist is not False:
        return False
    dropped = False
    for k in [k for k in changes if k in _COST_KEYS]:
        del changes[k]
        dropped = True
    return dropped


def _accept_unpersisted_costs(
    client: LiteLLMClient,
    cost_only_patched: list[tuple[Diff, dict[str, Any]]],
) -> int:
    """Verify freshly patched cost-only updates; ACCEPT what didn't stick.

    One re-list fetch covers the whole phase (non-dry-run callers only). A
    model's costs "stuck" iff EVERY drifted cost key is now present on live
    (a per-million echo still counts as present — the same helpers). If the
    proxy did not persist them, the Diff is downgraded to an accepted NOOP
    carrying the SERVER_DERIVED_PRICING message so the plan converges (dead
    PATCHes still go out every run). Returns the downgrade count for the
    caller's one-per-run warning.
    """
    live_after = {
        m["model_name"]: m
        for m in client.list_models()  # one fetch for the whole phase
        if m.get("model_name")
    }
    accepted = 0
    for diff, want in cost_only_patched:  # per-model acceptance
        remote_after = live_after.get(diff.name)
        if remote_after is None:
            # Race: the row vanished between the PATCH and the re-list.
            # Verification is impossible, and "can't verify" means "don't
            # accept": leave the Diff untouched (still an update) — the
            # next run re-evaluates the drift against whatever is live.
            continue
        # Costs "stuck" iff EVERY drifted cost key is now present on live
        # (a per-million echo still counts as present — the same helpers).
        stuck = all(
            _desired_cost_on_live(k, want[k], _comparable_model(remote_after))
            for k in diff.changes
        )
        if not stuck:
            # This proxy did not persist the costs — accept the cost-only
            # drift: downgrade UPDATE -> NOOP with the acceptance message
            # so the plan converges (dead PATCHes still sent every run).
            diff.action = Action.NOOP
            diff.message = _ACCEPTED_PRICING_MESSAGE
            accepted += 1
    return accepted


def reconcile_models(
    client: LiteLLMClient,
    spec_entries: list[dict[str, Any]],
    dry_run: bool = False,
) -> list[Diff]:
    diffs: list[Diff] = []
    live = {m["model_name"]: m for m in client.list_models() if m.get("model_name")}
    # Capability decision (litellm_as_code.capabilities, memoized once per
    # run): on >= 1.102 per-deployment costs are unmanageable, so apply AND
    # --dry-run converge; None -> the apply-then-verify fallback below.
    costs_persist = capabilities(client).model_costs_persist
    cost_accept_warned = False
    # (Diff, desired) pairs patched for cost-only drift this run; their Diffs
    # may be downgraded to accepted NOOPs by the verification phase below.
    cost_only_patched: list[tuple[Diff, dict[str, Any]]] = []

    for entry in spec_entries:
        name = entry["model_name"]
        remote = live.get(name)
        want = _desired_model_state(entry)

        if remote is None:
            diffs.append(Diff("model", name, Action.CREATE))
            if not dry_run:
                client.create_model(entry)
            continue
        have = _comparable_model(remote)
        changes = _model_changes(want, have)
        # Generation known up front: per-deployment costs are unmanageable on
        # this proxy — drop the cost keys from the change set so the plan
        # only describes deltas that can actually be applied.
        suppressed_costs = _suppress_cost_drift(changes, costs_persist)
        if changes:
            diff = Diff("model", name, Action.UPDATE, changes)
            diffs.append(diff)
            if not dry_run:
                model_id = _resolve_model_id(remote) or ""
                client.patch_model(model_id, entry)
                # >= 1.102 acceptance tracking (probe-unknown fallback only):
                # when the ONLY drift left is the two cost keys, the PATCH may
                # be a no-op server-side (dead PATCH is harmless on both
                # generations — v1.97 persists, >= 1.102 drops), so remember
                # it for the post-apply verification below. The PATCH body is
                # the spec entry verbatim (`entry`), so it may still carry the
                # cost keys the change set dropped — harmless either way
                # (>= 1.102 drops them server-side; <= 1.101 re-persists the
                # same values). Desired values come from the spec entry
                # (`want`, per-token). A model with
                # ANY non-cost drift is deliberately NOT tracked: a non-cost
                # change must never be swallowed by the acceptance phase.
                if set(changes) <= set(_COST_KEYS):
                    cost_only_patched.append((diff, want))
        else:
            diff = Diff("model", name, Action.NOOP, changes)
            if suppressed_costs:
                diff.message = _ACCEPTED_PRICING_MESSAGE
                if not cost_accept_warned:
                    warn(
                        "litellm-as-code",
                        "per-deployment model costs are not manageable on "
                        f"this proxy — {_ACCEPTED_PRICING_MESSAGE}",
                    )
                    cost_accept_warned = True
            diffs.append(diff)

    # ==== Apply-then-verify acceptance (non-dry-run only) ====
    # Dry-run asymmetry: verification would require actually PATCHing, so a
    # --dry-run plan keeps showing the cost update even on a proxy that will
    # not persist it (documented in the module docstring).
    if not dry_run and cost_only_patched:
        accepted = _accept_unpersisted_costs(client, cost_only_patched)
        if accepted:
            warn(
                "litellm-as-code",
                f"{accepted} model(s): cost-only drift accepted — "
                f"{_ACCEPTED_PRICING_MESSAGE}",
            )

    return diffs
