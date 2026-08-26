"""Export a live LiteLLM proxy's runtime state as a declarative YAML spec.

The inverse of the reconciler: instead of diffing a spec against the live API,
`export` interrogates the live API and writes a spec that — fed back to
`litellm-as-code <spec>` — reproduces the deployment.

Fidelity model (must stay consistent with AGENTS.md §2, §3, §5):

- Only **comparable (manageable)** fields are exported — the same fields the
  reconcilers diff (see the COMPARABLE lists in `resources/*.py`). Runtime
  metrics (``spend``, ``updated_at``, ``status``, ``budget_reset_at``) and
  server-injected defaults (``default_user_id``, ``tpm_limit_type``,
  ``rpm_limit_type``, inferred ``mode``) are never adopted into desired state.
- **Secrets are write-once** and can never be re-read:
  - credential ``credential_values`` come back masked (never plaintext) — the
    export emits an empty ``{}`` plus an inline placeholder comment and a WARN
    per credential so the operator fills it in.
  - a key's raw ``key`` value comes back absent (the API stores only the hashed
    token) — the export omits it (re-apply mints a fresh key) and WARNs.
    Sending ``key`` on re-apply is what would reproduce a *specific* key, which
    is deliberately impossible.
- Model cost conversion: the API returns per-token costs under
  ``model_info``/``litellm_params``; the spec expresses per-million, so we
  convert back (×1e6) — the exact inverse of
  ``resources/models._cost_to_per_token``.

Ordering of sections in the output mirrors the reconciler's fixed converge
order: budgets -> models -> credentials -> organizations (+members) -> users ->
teams (+members) -> virtual_keys -> guardrails -> policies.

Every exported spec passes ``load_spec`` validation (see ``export_spec``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .api import LiteLLMClient
from .log import warn
from .spec import load_spec

# Sections in reconcile order; the exporter emits exactly these, omitting any
# that are empty so a fresh proxy exports a minimal spec.
_SECTIONS = (
    "budgets",
    "models",
    "credentials",
    "organizations",
    "users",
    "teams",
    "virtual_keys",
    "guardrails",
    "policies",
)

_HEADER = """\
# litellm-as-code — exported from a live LiteLLM proxy.
#
# Regenerated from the proxy's runtime (DB-backed) state; re-applying it
# reproduces the deployment. It does NOT capture the proxy's startup
# config.yaml (general_settings / litellm_settings / router_settings).
#
# Secrets are write-once and are NEVER re-read from the API, so this file
# cannot contain them. Replace the placeholders below before applying:
#   - credentials[].credential_values  (API returns masked values)
#   - virtual_keys[].key               (omitted; re-applying mints a fresh key)
"""

# Inline YAML comments injected next to the entry whose secret is missing.
_CREDENTIAL_VALUES_COMMENT = (
    "# <fill: credential_values> — API returns these masked; set e.g.\n"
    "#   api_key / api_base here so the credential works on re-apply"
)
_KEY_COMMENT = (
    "# <fill: key> — write-once, omitted: re-applying this spec mints a fresh key"
)


def _pick(src: dict[str, Any], keys: list[str]) -> dict[str, Any]:
    """Copy present non-None comparable fields from a live row."""
    out: dict[str, Any] = {}
    for k in keys:
        if src.get(k) is not None:
            out[k] = src[k]
    return out


def _empty_collections(
    d: dict[str, Any], keep: set[str] | None = None
) -> dict[str, Any]:
    """Drop empty collections ([] / {}) — the spec omits them and the
    reconciler treats omitted == empty (see diff._equiv). Keeps exports clean.

    Keys in ``keep`` are retained even when empty (used for
    ``credential_values``, where an explicit empty map marks the fill-in spot).
    """
    keep = keep or set()
    return {k: v for k, v in d.items() if k in keep or v not in ([], {})}


def _emit_warn(section: str, name: str, detail: str) -> None:
    warn("export", f"{section}[{name}]: {detail}")


# -- budgets ----------------------------------------------------------------

_BUDGET_KEYS = [
    "budget_id",
    "max_budget",
    "soft_budget",
    "max_parallel_requests",
    "tpm_limit",
    "rpm_limit",
    "model_max_budget",
    "budget_duration",
]


def _export_budgets(client: LiteLLMClient) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for b in client.list_budgets():
        bid = b.get("budget_id")
        if not bid:
            _emit_warn("budgets", str(bid), "skipping budget row without budget_id")
            continue
        entry = _pick(b, _BUDGET_KEYS)
        if entry:
            out.append(_empty_collections(entry))
    return out


# -- models ----------------------------------------------------------------

_MODEL_INFO_KEYS = ["mode", "base_model", "tier"]
_MODEL_LITELLM_KEYS = ["custom_llm_provider", "model", "litellm_credential_name"]


def _export_models(client: LiteLLMClient) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in client.list_models():
        name = m.get("model_name")
        if not name:
            _emit_warn("models", str(name), "skipping model row without model_name")
            continue
        mi = m.get("model_info") or {}
        lp = m.get("litellm_params") or {}

        model_info: dict[str, Any] = {}
        for k in _MODEL_INFO_KEYS:
            if mi.get(k) is not None:
                model_info[k] = mi[k]

        litellm_params: dict[str, Any] = {}
        for k in _MODEL_LITELLM_KEYS:
            v = lp.get(k)
            if v is None:
                v = mi.get(k)
            if v is not None:
                litellm_params[k] = v

        _export_model_costs(model_info, mi, lp)

        entry: dict[str, Any] = {"model_name": name}
        if model_info:
            entry["model_info"] = _empty_collections(model_info)
        if litellm_params:
            entry["litellm_params"] = _empty_collections(litellm_params)
        out.append(entry)
    return out


def _export_model_costs(
    model_info: dict[str, Any], mi: dict[str, Any], lp: dict[str, Any]
) -> None:
    """Carry costs back to per-million (spec convention), inverse of the
    reconciler's ÷1e6. The proxy may report the same cost per-token and/or
    per-million under model_info or litellm_params; per-token wins (it is the
    authoritative stored value; the per-million echo can be 0 for DB-backed
    models whose cost table isn't loaded)."""
    for per_token, per_million in (
        ("input_cost_per_token", "input_cost_per_million_tokens"),
        ("output_cost_per_token", "output_cost_per_million_tokens"),
    ):
        v = mi.get(per_token)
        if v is None:
            v = lp.get(per_token)
        if v is None:
            v = mi.get(per_million)
        if v is None:
            v = lp.get(per_million)
        if isinstance(v, (int, float)):
            model_info[per_million] = v * 1_000_000.0


# -- credentials -----------------------------------------------------------

_CREDENTIAL_KEYS = ["credential_name", "credential_info", "model_id"]


def _export_credentials(client: LiteLLMClient) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for c in client.list_credentials():
        name = c.get("credential_name")
        if not name:
            _emit_warn("credentials", str(name), "skipping row without credential_name")
            continue
        entry = _pick(c, _CREDENTIAL_KEYS)
        entry.setdefault("credential_info", {})
        # credential_values are masked on read — never re-exported. Emit an
        # empty {} (valid & re-applyable) + a loud WARN; the inline comment
        # marks the spot for the operator to fill.
        entry["credential_values"] = {}
        _emit_warn(
            "credentials",
            name,
            "credential_values are masked by the API and not exported; "
            "fill them in manually (write-once)",
        )
        out.append(_empty_collections(entry, keep={"credential_values"}))
    return out


# -- organizations ---------------------------------------------------------

_ORG_KEYS = ["organization_id", "organization_alias", "models"]


def _export_organizations(client: LiteLLMClient) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for o in client.list_organizations():
        org_id = o.get("organization_id")
        if not org_id:
            _emit_warn("organizations", str(org_id), "skipping row without organization_id")
            continue
        entry = _pick(o, _ORG_KEYS)
        members = o.get("members") or []
        roles = []
        for mm in members:
            if not mm.get("user_id"):
                continue
            roles.append(
                {"user_id": mm["user_id"], "role": mm.get("user_role", "internal_user")}
            )
        if roles:
            entry["members_with_roles"] = roles
        if entry:
            out.append(_empty_collections(entry))
    return out


# -- users -----------------------------------------------------------------

_USER_KEYS = ["user_id", "user_alias", "user_email", "user_role", "auto_create_key"]


def _export_users(client: LiteLLMClient) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for u in client.list_users():
        uid = u.get("user_id")
        # The server-internal default_user_id row is not a user the operator
        # manages; skip it (see roles.py / AGENTS.md §4).
        if not uid or uid == "default_user_id":
            continue
        entry = _pick(u, _USER_KEYS)
        if entry:
            out.append(_empty_collections(entry))
    return out


# -- teams -----------------------------------------------------------------

_TEAM_KEYS = ["team_id", "team_alias", "organization_id", "max_budget", "budget_duration", "models"]


def _export_teams(client: LiteLLMClient) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for t in client.list_teams():
        team_id = t.get("team_id")
        if not team_id:
            _emit_warn("teams", str(t.get("team_alias")), "skipping row without team_id")
            continue
        entry = _pick(t, _TEAM_KEYS)
        # Nested server budget table is never desired state.
        entry.pop("litellm_budget_table", None)
        members = _export_team_members(client, team_id)
        if members:
            entry["members_with_roles"] = members
        if entry:
            out.append(_empty_collections(entry))
    return out


def _export_team_members(client: LiteLLMClient, team_id: str) -> list[dict[str, Any]]:
    """members_with_roles come from GET /team/info (the /v2/team/list rows do
    not carry the member list)."""
    try:
        info = client.get_team_info(team_id)
    except Exception as e:  # noqa: BLE001 — a team-info 404 shouldn't kill the export
        _emit_warn("teams", team_id, f"could not read members: {e}")
        return []
    members = info.get("members_with_roles") or []
    out: list[dict[str, Any]] = []
    for m in members:
        if not m.get("user_id"):
            continue
        out.append({"user_id": m["user_id"], "role": m.get("role", "user")})
    return out


# -- virtual keys ----------------------------------------------------------

_KEY_KEYS = ["key_alias", "user_id", "team_id", "models", "max_budget", "budget_duration", "allowed_routes"]


def _export_keys(client: LiteLLMClient) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for k in client.list_keys():
        alias = k.get("key_alias")
        if not alias:
            _emit_warn("virtual_keys", str(alias), "skipping key without key_alias")
            continue
        entry = _pick(k, _KEY_KEYS)
        # Raw `key` is write-once and never read back (only the hash is
        # stored). Omit it — re-apply mints a fresh key — and WARN.
        _emit_warn(
            "virtual_keys",
            alias,
            "raw key value is not readable from the API and is not exported; "
            "re-applying mints a new key",
        )
        out.append(_empty_collections(entry))
    return out


# -- guardrails ------------------------------------------------------------

_GUARDRAIL_KEYS = ["guardrail_name", "litellm_params", "guardrail_info"]


def _export_guardrails(client: LiteLLMClient) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for g in client.list_guardrails():
        # Config-file (startup) guardrails have no guardrail_id; the
        # reconciler never manages them (see guardrails.py).
        if not g.get("guardrail_id"):
            continue
        name = g.get("guardrail_name")
        if not name:
            _emit_warn("guardrails", str(name), "skipping row without guardrail_name")
            continue
        entry = _pick(g, _GUARDRAIL_KEYS)
        if entry:
            out.append(_empty_collections(entry))
    return out


# -- policies --------------------------------------------------------------

_POLICY_KEYS = ["policy_name", "inherit", "description", "guardrails_add", "guardrails_remove"]


def _export_policies(client: LiteLLMClient) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in client.list_policies():
        # Skip config-file policies (definition_location == "config"); they are
        # startup-only and not reconciled (see policies.py).
        if p.get("definition_location", "db") != "db":
            continue
        if not p.get("policy_id"):
            continue
        name = p.get("policy_name")
        if not name:
            _emit_warn("policies", str(name), "skipping row without policy_name")
            continue
        entry = _pick(p, _POLICY_KEYS)
        if entry:
            out.append(_empty_collections(entry))
    return out


# -- assembly --------------------------------------------------------------


def build_spec(client: LiteLLMClient) -> dict[str, Any]:
    """Read the live proxy and return a spec dict (reconcile order)."""
    sections = {
        "budgets": _export_budgets(client),
        "models": _export_models(client),
        "credentials": _export_credentials(client),
        "organizations": _export_organizations(client),
        "users": _export_users(client),
        "teams": _export_teams(client),
        "virtual_keys": _export_keys(client),
        "guardrails": _export_guardrails(client),
        "policies": _export_policies(client),
    }
    return {name: sections[name] for name in _SECTIONS if sections[name]}


def _insert_entry_comments(text: str, key: str, comment_lines: list[str]) -> str:
    """Insert comment lines right after each list-entry line carrying `key:`.

    Only used for unambiguous per-entry identity keys (``credential_name``,
    ``key_alias``) which never occur nested, so matching on a line whose
    content starts with the key (after the optional `- ` list marker) is safe.
    """
    comment = "\n".join(comment_lines)
    lines = text.splitlines()
    out: list[str] = []
    for line in lines:
        out.append(line)
        content = line.lstrip().lstrip("-").lstrip()
        if content.startswith(key + ":"):
            out.append(comment)
    return "\n".join(out)


def export_spec(client: LiteLLMClient, out_path: str | Path) -> dict[str, Any]:
    """Export the live proxy to ``out_path`` as a re-applyable YAML spec.

    Returns the spec dict (for tests / callers). The emitted file is
    guaranteed to pass ``load_spec`` validation.
    """
    spec = build_spec(client)

    # Dump section-by-section so per-section comment insertion stays scoped
    # and a section never ends up indented under its predecessor.
    blocks: list[str] = []
    for section in _SECTIONS:
        entries = spec.get(section)
        if not entries:
            continue
        block = yaml.safe_dump(
            {section: entries},
            sort_keys=False,
            default_flow_style=False,
            allow_unicode=True,
        )
        if section == "credentials":
            block = _insert_entry_comments(
                block, "credential_name", _CREDENTIAL_VALUES_COMMENT.splitlines()
            )
        elif section == "virtual_keys":
            block = _insert_entry_comments(
                block, "key_alias", _KEY_COMMENT.splitlines()
            )
        blocks.append(block)

    Path(out_path).write_text(_HEADER + "\n".join(blocks), encoding="utf-8")

    # Self-check: the generated file must round-trip through load_spec so a
    # broken export is caught before the operator applies it.
    load_spec(out_path)
    return spec
