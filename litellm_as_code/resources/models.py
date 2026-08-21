"""Model reconciler.

Identity: `model_name` (e.g. "logarithmus/large").
Comparable fields: the managed subset of model_info/litellm_params.
Mutation: POST /model/new | PATCH /model/{id}/update | POST /model/delete.

Cost conversion: the spec may express `input_cost_per_million_tokens` /
`output_cost_per_million_tokens` (ecosystem convention); the reconciler
converts to per-token when sending to the API, and back when comparing.
"""

from __future__ import annotations

from typing import Any

from ..api import LiteLLMClient
from ..types import Action, Diff


def _cost_to_per_token(v: Any) -> float | None:
    """Convert a per-million value to per-token, or passthrough."""
    if v is None:
        return None
    return float(v) / 1_000_000.0


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


def reconcile_models(
    client: LiteLLMClient,
    spec_entries: list[dict[str, Any]],
    dry_run: bool = False,
) -> list[Diff]:
    diffs: list[Diff] = []
    live = {m["model_name"]: m for m in client.list_models() if m.get("model_name")}

    for entry in spec_entries:
        name = entry["model_name"]
        remote = live.get(name)
        mi = entry.get("model_info") or {}
        lp = entry.get("litellm_params") or {}

        # convert per-million -> per-token for the compare
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
        # drop None-valued entries from desired so they don't read as drift
        want = {k: v for k, v in want.items() if v is not None}

        if remote is None:
            diffs.append(Diff("model", name, Action.CREATE))
            if not dry_run:
                client.create_model(entry)
            continue

        have = _comparable_model(remote)
        # treat remote "model" <provider>/<base> as equal to our litellm_params.model
        # and compare costs with unit tolerance (per-token vs per-million/1e6,
        # 0 == 0.0, int vs float)
        changes = {}
        for k in want:
            h = have.get(k)
            if k == "input_cost_per_token":
                equal = _per_token_equal(want[k], have, "input_cost_per_million_tokens")
            elif k == "output_cost_per_token":
                equal = _per_token_equal(want[k], have, "output_cost_per_million_tokens")
            else:
                equal = want[k] == h
            if not equal:
                changes[k] = (want[k], h)
        diffs.append(
            Diff("model", name, Action.UPDATE if changes else Action.NOOP, changes)
        )
        if changes and not dry_run:
            model_id = _resolve_model_id(remote) or ""
            client.patch_model(model_id, entry)

    return diffs
