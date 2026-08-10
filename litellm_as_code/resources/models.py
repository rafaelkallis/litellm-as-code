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
    for k in (
        "custom_llm_provider",
        "model",
        "litellm_credential_name",
        "input_cost_per_token",
        "output_cost_per_token",
    ):
        if lp.get(k) is not None:
            out[k] = lp[k]
    return out


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
        changes = {
            k: (want[k], have.get(k)) for k in want if have.get(k) != want[k]
        }
        diffs.append(
            Diff("model", name, Action.UPDATE if changes else Action.NOOP, changes)
        )
        if changes and not dry_run:
            client.patch_model(remote_id := _resolve_model_id(remote) or "", entry)

    return diffs
