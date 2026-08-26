"""Exporter tests against the fake proxy.

Verifies the inverse-of-reconciler mapping: comparable fields are exported
back in spec form (per-million costs, members un-nested, secrets omitted)
and that an exported spec round-trips: re-applying it to the same proxy is a
no-op (0 to create, 0 to update).
"""

from __future__ import annotations

import json

import pytest
import yaml

from litellm_as_code.exporter import build_spec, export_spec
from litellm_as_code.reconciler import reconcile
from litellm_as_code.spec import load_spec
from litellm_as_code.types import Action

from tests import make_fake_client

# Seed spec that touches every reconcilable section (mirrors test_reconciler's
# FULL_SPEC so exports are exercised across the whole surface).
SOURCE_SPEC = {
    "budgets": [
        {"budget_id": "b1", "max_budget": 100.0, "soft_budget": 80.0, "budget_duration": "30d"},
    ],
    "models": [
        {
            "model_name": "org/chat",
            "model_info": {
                "id": "m1",
                "mode": "chat",
                "base_model": "b1",
                "input_cost_per_million_tokens": 3.0,
                "output_cost_per_million_tokens": 15.0,
            },
            "litellm_params": {"model": "hosted_vllm/b1", "litellm_credential_name": "c1"},
        }
    ],
    "credentials": [
        {
            "credential_name": "c1",
            "credential_info": {"custom_llm_provider": "hosted_vllm"},
            "credential_values": {"api_key": "sk-provider", "api_base": "http://v:8000"},
        }
    ],
    "organizations": [
        {
            "organization_id": "org-1",
            "organization_alias": "acme",
            "members_with_roles": [{"user_id": "u1", "role": "org_admin"}],
        }
    ],
    "users": [
        {
            "user_id": "u1",
            "user_alias": "admin",
            "user_role": "proxy_admin",
            "auto_create_key": "false",
        }
    ],
    "teams": [
        {
            "team_id": "team-prod",
            "team_alias": "prod",
            "members_with_roles": [{"user_id": "u1", "role": "admin"}],
        }
    ],
    "virtual_keys": [
        {"key_alias": "k1", "key": "sk-abc", "user_id": "u1"},
    ],
    "guardrails": [
        {
            "guardrail_name": "pii-guard",
            "litellm_params": {"guardrail": "presidio", "mode": "pre_call"},
            "guardrail_info": {"description": "PII masking"},
        }
    ],
    "policies": [
        {"policy_name": "global-baseline", "guardrails_add": ["pii-guard"]},
    ],
}


@pytest.fixture
def populated():
    """A fake proxy that has been converged to SOURCE_SPEC."""
    client, fake = make_fake_client()
    return client, fake


@pytest.fixture
def converged(tmp_path, populated):
    client, fake = populated
    spec = tmp_path / "spec.yml"
    spec.write_text(json.dumps(SOURCE_SPEC))
    plan = reconcile(str(spec), client, dry_run=False)
    assert plan.create_count > 0
    return client, fake


def test_export_round_trip_is_noop(converged, tmp_path):
    """Export a converged proxy, then re-apply the export: nothing to do."""
    client, fake = converged
    out = tmp_path / "export.yml"

    exported = export_spec(client, out)

    # The export validates through load_spec (export_spec self-checks, but be
    # explicit for the test).
    assert load_spec(out) is not None

    plan = reconcile(str(out), client, dry_run=False)
    assert plan.create_count == 0, [str(d) for d in plan.diffs if d.action is Action.CREATE]
    assert plan.update_count == 0, [str(d) for d in plan.diffs if d.action is Action.UPDATE]


def test_export_sections_in_reconcile_order(converged, tmp_path):
    """" All nine sections are present in the fixed converge order."""
    client, fake = converged
    exported = build_spec(client)
    assert list(exported) == [
        "budgets",
        "models",
        "credentials",
        "organizations",
        "users",
        "teams",
        "virtual_keys",
        "guardrails",
        "policies",
    ]


def test_export_omits_secrets(converged, tmp_path):
    """Masked credential values & raw keys are never exported (write-once)."""
    client, fake = converged
    exported = build_spec(client)

    assert exported["credentials"][0]["credential_name"] == "c1"
    # The API only ever surfaces masked values; the export emits an empty {}.
    assert exported["credentials"][0]["credential_values"] == {}
    # model_id is a comparable field but the fake never echoes it; the real
    # API doesn't either, so it may be absent — that's fine.

    # Every key entry must NOT carry the raw `key`.
    for k in exported["virtual_keys"]:
        assert "key" not in k
        assert k["key_alias"] == "k1"


def test_export_writes_placeholder_comments(converged, tmp_path):
    """The YAML file carries inline fill-in markers for secrets."""
    client, fake = converged
    out = tmp_path / "export.yml"
    export_spec(client, out)
    text = out.read_text()
    assert "# <fill: credential_values>" in text
    assert "# <fill: key>" in text
    assert "credential_name: c1" in text
    assert "key_alias: k1" in text


def test_export_model_costs_back_to_per_million(converged, tmp_path):
    """Per-token costs stored by the proxy are exported per-million (spec)."""
    client, fake = converged
    exported = build_spec(client)
    model = exported["models"][0]
    mi = model["model_info"]
    # 3.0 / 1e6 stored per-token -> exported back as 3.0 per-million.
    assert mi["input_cost_per_million_tokens"] == pytest.approx(3.0)
    assert mi["output_cost_per_million_tokens"] == pytest.approx(15.0)
    # The server-minted model_info.id is not desired state.
    assert "id" not in mi
    assert model["litellm_params"]["litellm_credential_name"] == "c1"


def test_export_team_members_from_team_info(converged, tmp_path):
    """/team/info member roles surface as team.members_with_roles."""
    client, fake = converged
    exported = build_spec(client)
    team = exported["teams"][0]
    assert team["team_id"] == "team-prod"
    assert team["members_with_roles"] == [{"user_id": "u1", "role": "admin"}]


def test_export_org_members(converged, tmp_path):
    """/organization/list `members` map to organization.members_with_roles."""
    client, fake = converged
    exported = build_spec(client)
    org = next(o for o in exported["organizations"] if o["organization_id"] == "org-1")
    assert org["members_with_roles"] == [{"user_id": "u1", "role": "org_admin"}]


def test_export_skips_config_only_guardrails_and_policies(converged):
    """Startup config.yaml rows (no guardrail_id / definition_location=config)
    are out of scope and must not appear in the export."""
    client, fake = converged
    # Emulate the proxy's /v2/guardrails/list returning a config-file row.
    fake.guardrails["cfg-gr"] = {"guardrail_name": "cfg-gr", "litellm_params": {}}
    # _list_guardrails only lists rows in fake.guardrails; give it an id so it
    # looks DB-backed OR leave id-less to mimic config-only rows.
    fake.policies["cfg-pol"] = {
        "policy_name": "cfg-pol",
        "description": "config",
        "definition_location": "config",
    }

    exported = build_spec(client)
    names = [g["guardrail_name"] for g in exported["guardrails"]]
    assert "pii-guard" in names
    assert "cfg-gr" not in names
    pol_names = [p["policy_name"] for p in exported["policies"]]
    assert "global-baseline" in pol_names
    assert "cfg-pol" not in pol_names


def test_export_skips_noop_sections_on_empty_proxy():
    """A fresh proxy with no DB rows exports a minimal (section-less) spec."""
    client, fake = make_fake_client()
    exported = build_spec(client)
    assert exported == {}


def test_export_excludes_runtime_metrics(converged, tmp_path):
    """spend / budget_reset_at / created_at / status never become desired state."""
    client, fake = converged
    # poke runtime fields onto every live row (emulating the real API)
    for u in fake.users.values():
        u["spend"] = 12.5
        u["status"] = "ok"
    for b in fake.budgets.values():
        b["spend"] = 3.0
        b["budget_reset_at"] = "2026-09-01T00:00:00Z"
    for t in fake.teams.values():
        t["spend"] = 1.0
    for k in fake.keys.values():
        k["spend"] = 0.5
        k["last_active"] = "2026-08-26T00:00:00Z"

    exported = build_spec(client)

    def _dump(model):
        return yaml.safe_dump(model)

    for section in ("budgets", "models", "credentials", "organizations", "users",
                    "teams", "virtual_keys", "guardrails", "policies"):
        for entry in exported.get(section, []):
            dump = _dump(entry)
            assert "spend" not in dump
            assert "budget_reset_at" not in dump
            assert "status" not in dump
            assert "last_active" not in dump
            assert "created_at" not in dump
            assert "updated_at" not in dump


def test_exported_user_keeps_identity(converged, tmp_path):
    """user_id identity is preserved so memberships still resolve on re-apply."""
    client, fake = converged
    exported = build_spec(client)
    users = {u["user_id"]: u for u in exported["users"]}
    assert users["u1"]["user_role"] == "proxy_admin"


def test_export_then_reconcile_is_noop_via_cli_flow(tmp_path, converged):
    """The exported file, run through the real reconciler flow, is a no-op."""
    client, fake = converged
    out = tmp_path / "exported.yml"
    export_spec(client, out)

    # Same proxy, run reconcile on the exported file (this is exactly what
    # `litellm-as-code exported.yml` does minus the CLI arg plumbing).
    plan = reconcile(str(out), client, dry_run=False)
    assert plan.create_count == 0
    assert plan.update_count == 0
    # And a dry-run on the converged state is clean (exit-0-equivalent plan).
    plan2 = reconcile(str(out), client, dry_run=True)
    assert plan2.create_count == 0 and plan2.update_count == 0


def test_export_uses_spec_identity_forms(converged, tmp_path):
    """Alias-only orgs export via organization_alias (spec-legal identity)."""
    client, fake = converged
    # Add an alias-only organization (like the fake's _create_organization
    # minting org-N ids).
    client.create_organization({"organization_alias": "alias-only"})
    exported = build_spec(client)
    org = next(
        o for o in exported["organizations"] if o.get("organization_alias") == "alias-only"
    )
    assert org.get("organization_id")  # fake mints an org id on create
