"""End-to-end reconciler tests against the fake proxy.

Mirrors `litellm_as_code/` 1:1 like the ecosystem's test layout, but all
mock-only (no real LiteLLM calls).
"""

from __future__ import annotations

import json

import pytest

from litellm_as_code.reconciler import reconcile
from litellm_as_code.types import Action

from tests import make_fake_client

SPEC = {
    "users": [
        {"user_id": "u1", "user_alias": "admin", "user_role": "proxy_admin", "auto_create_key": "false"},
        {"user_id": "u2", "user_alias": "worker", "user_role": "internal_user_viewer", "auto_create_key": "false"},
    ],
    "teams": [
        {
            "team_id": "team-prod",
            "team_alias": "prod",
            "members_with_roles": [
                {"user_id": "u1", "role": "admin"},
                {"user_id": "u2", "role": "user"},
            ],
        }
    ],
    "virtual_keys": [
        {"key_alias": "k1", "key": "sk-abc", "user_id": "u1"},
    ],
    "credentials": [
        {
            "credential_name": "c1",
            "credential_info": {"custom_llm_provider": "hosted_vllm"},
            "credential_values": {"api_key": "sk-provider", "api_base": "http://v:8000"},
        }
    ],
    "models": [
        {
            "model_name": "org/chat",
            "model_info": {"id": "m1", "mode": "chat", "base_model": "b1", "input_cost_per_million_tokens": 3.0},
            "litellm_params": {"model": "hosted_vllm/b1", "litellm_credential_name": "c1"},
        }
    ],
}

# Full-surface spec: exercises every reconcilable section at once.
FULL_SPEC = {
    **SPEC,
    "budgets": [
        {"budget_id": "b1", "max_budget": 100.0, "budget_duration": "30d"},
    ],
    "organizations": [
        {
            "organization_id": "org-1",
            "organization_alias": "acme",
            "members_with_roles": [
                {"user_id": "u1", "role": "org_admin"},
            ],
        }
    ],
    "guardrails": [
        {
            "guardrail_name": "pii-guard",
            "litellm_params": {"guardrail": "presidio", "mode": "pre_call"},
        }
    ],
    "policies": [
        {
            "policy_name": "global-baseline",
            "guardrails_add": ["pii-guard"],
        }
    ],
}


@pytest.fixture
def ctx():
    client, fake = make_fake_client()
    return client, fake


def test_first_run_creates_everything(ctx, tmp_path):
    client, fake = ctx
    spec = tmp_path / "spec.yml"
    spec.write_text(json.dumps(SPEC))

    plan = reconcile(str(spec), client, dry_run=False)

    assert plan.create_count == 8  # 2 users + team + 2 members + key + cred + model
    assert plan.update_count == 0
    assert plan.noop_count == 0


def test_second_run_is_noop(ctx, tmp_path):
    client, fake = ctx
    spec = tmp_path / "spec.yml"
    spec.write_text(json.dumps(SPEC))

    reconcile(str(spec), client, dry_run=False)
    plan = reconcile(str(spec), client, dry_run=False)

    assert plan.create_count == 0
    assert plan.update_count == 0
    # Members converge silently (no-op diffs are only emitted for create/update
    # of members); so: 2 users + 1 team + 1 key + 1 cred + 1 model = 6 no-ops.
    assert plan.noop_count == 6

def test_dry_run_does_not_mutate(ctx, tmp_path):
    client, fake = ctx
    spec = tmp_path / "spec.yml"
    spec.write_text(json.dumps(SPEC))

    plan = reconcile(str(spec), client, dry_run=True)

    # In dry-run the team doesn't exist, so its members fold into a single
    # "team/* members" placeholder diff -> 7 create diffs (not 8 per-member).
    assert plan.create_count == 7
    assert len(fake.users) == 0
    assert len(fake.teams) == 0
    assert len(fake.keys) == 0
    assert len(fake.credentials) == 0
    assert len(fake.models) == 0


def test_drift_detects_role_change(ctx, tmp_path):
    client, fake = ctx
    spec = tmp_path / "spec.yml"
    spec.write_text(json.dumps(SPEC))
    reconcile(str(spec), client, dry_run=False)

    # user u2 role changes
    changed = json.loads(spec.read_text())
    changed["users"][1]["user_role"] = "proxy_admin"
    spec.write_text(json.dumps(changed))

    plan = reconcile(str(spec), client, dry_run=False)
    updates = {d.name: d for d in plan.diffs if d.action is Action.UPDATE}
    assert "worker" in updates
    assert "user_role" in updates["worker"].changes


def test_key_is_not_rotated_on_update(ctx, tmp_path):
    client, fake = ctx
    spec = tmp_path / "spec.yml"
    spec.write_text(json.dumps(SPEC))
    reconcile(str(spec), client, dry_run=False)

    original_key = fake.keys["k1"]["key"]

    # change a non-secret key field (e.g. add models), keep same key value
    changed = json.loads(spec.read_text())
    changed["virtual_keys"][0]["models"] = ["org/chat"]
    spec.write_text(json.dumps(changed))

    reconcile(str(spec), client, dry_run=False)
    assert fake.keys["k1"]["key"] == original_key  # no rotation


def test_credential_secret_is_reasserted_on_patch(ctx, tmp_path):
    client, fake = ctx
    spec = tmp_path / "spec.yml"
    spec.write_text(json.dumps(SPEC))
    reconcile(str(spec), client, dry_run=False)

    # Change a comparable field AND update a secret value together. Because a
    # comparable change triggers a PATCH, the updated credential_values are
    # re-asserted too (they cannot be diffed against live — the API masks them).
    changed = json.loads(spec.read_text())
    changed["credentials"][0]["credential_info"]["custom_llm_provider"] = "openai"
    changed["credentials"][0]["credential_values"]["api_key"] = "sk-rotated"
    spec.write_text(json.dumps(changed))

    plan = reconcile(str(spec), client, dry_run=False)
    updates = [d for d in plan.diffs if d.action is Action.UPDATE]
    assert any(d.name == "c1" for d in updates)
    # Server-side values were re-asserted (read back masked proves persistence).
    assert client.get_credential_by_name("c1")["credential_values"] == {
        "api_key": "sk-r***",
        "api_base": "http***",
    }


def test_team_member_role_update_and_removal(ctx, tmp_path):
    client, fake = ctx
    spec = tmp_path / "spec.yml"
    spec.write_text(json.dumps(SPEC))
    reconcile(str(spec), client, dry_run=False)

    # change u2's role and drop u1 from the team
    changed = json.loads(spec.read_text())
    changed["teams"][0]["members_with_roles"] = [
        {"user_id": "u2", "role": "admin"},
    ]
    spec.write_text(json.dumps(changed))

    plan = reconcile(str(spec), client, dry_run=False)
    updates = {d.name: d for d in plan.diffs if d.action is Action.UPDATE}

    # role update for u2 within the team
    assert "prod/u2" in updates
    assert updates["prod/u2"].changes["role"] == ("user", "admin")
    # removal of u1 emits an UPDATE diff (member deleted)
    assert "prod/u1" in updates

    assert fake.team_members[("team-prod", "u2")] == "admin"
    assert ("team-prod", "u1") not in fake.team_members


def test_model_cost_is_stable_across_reconciles(ctx, tmp_path):
    """Per-million cost in the spec must map onto the per-token cost the fake
    proxy stores — and back — so a second run is a no-op (no perpetual drift)."""
    client, fake = ctx
    spec = tmp_path / "spec.yml"
    spec.write_text(json.dumps(SPEC))

    plan1 = reconcile(str(spec), client, dry_run=False)
    assert any(d.name == "org/chat" and d.action is Action.CREATE for d in plan1.diffs)

    plan2 = reconcile(str(spec), client, dry_run=False)
    model_diffs = [d for d in plan2.diffs if d.resource_type == "model"]
    assert all(d.action is Action.NOOP for d in model_diffs)

    # the fake stored cost per-token, exactly like the real proxy
    lp = fake.models["org/chat"]["litellm_params"]
    assert lp["input_cost_per_token"] == 3.0 / 1_000_000.0


def test_model_nonzero_cost_converges_when_live_echoes_per_million(ctx, tmp_path):
    """Real-proxy behavior found via permutation testing: with
    STORE_MODEL_IN_DB the server echoes the per-million cost back under
    model_info AND a recomputed per-token value that is 0 for DB-backed
    models. A non-zero spec cost must still converge (compare in either
    unit), not flag an update every run."""
    client, fake = ctx
    spec = tmp_path / "spec.yml"
    spec.write_text(json.dumps(SPEC))
    reconcile(str(spec), client, dry_run=False)

    # Mimic the real proxy: keep per-million in model_info, echo per-token as
    # 0 (recomputed lazily, cost table not loaded).
    m = fake.models["org/chat"]
    lp = m["litellm_params"]
    # real proxy's read-back model_info carries both per-million + per-token
    m["model_info"] = {
        **m.get("model_info", {}),
        "input_cost_per_million_tokens": 3.0,
        "output_cost_per_million_tokens": 0.0,
        "input_cost_per_token": 0,
        "output_cost_per_token": 0,
    }
    lp["input_cost_per_token"] = 0
    lp["output_cost_per_token"] = 0

    plan = reconcile(str(spec), client, dry_run=False)
    model_diffs = [d for d in plan.diffs if d.resource_type == "model"]
    assert all(d.action is Action.NOOP for d in model_diffs), model_diffs


def test_full_surface_first_run_creates_everything(ctx, tmp_path):
    """All reconcilable sections create their resources on the first run:
    2 users + team + 2 members + key + cred + model + budget + org + 1 org
    member + guardrail + policy."""
    client, fake = ctx
    spec = tmp_path / "spec.yml"
    spec.write_text(json.dumps(FULL_SPEC))

    plan = reconcile(str(spec), client, dry_run=False)

    assert plan.create_count == 13
    assert plan.update_count == 0
    assert fake.budgets["b1"]["max_budget"] == 100.0
    assert fake.org_members[("org-1", "u1")] == "org_admin"
    assert "pii-guard" in fake.guardrails
    assert "global-baseline" in fake.policies


def test_full_surface_second_run_is_noop(ctx, tmp_path):
    client, fake = ctx
    spec = tmp_path / "spec.yml"
    spec.write_text(json.dumps(FULL_SPEC))

    reconcile(str(spec), client, dry_run=False)
    plan = reconcile(str(spec), client, dry_run=False)

    # every new resource type converges to no-op on the second run
    for rtype in ("budget", "organization", "guardrail", "policy"):
        diffs = [d for d in plan.diffs if d.resource_type == rtype]
        assert all(d.action is Action.NOOP for d in diffs), rtype
    assert plan.create_count == 0
    assert plan.update_count == 0


def test_models_are_reconciled_before_credentials_with_model_id(ctx, tmp_path):
    """A credential that binds a model via model_id must be created AFTER the
    model exists — POST /credentials 404s on unknown model_id (found live:
    `404 Model not found`). The fixed ordering is models -> credentials."""
    client, fake = ctx
    spec = tmp_path / "spec.yml"
    model_id = "bbbbbbbb-2222-3333-4444-555555555555"
    spec.write_text(
        json.dumps(
            {
                "credentials": [
                    {
                        "credential_name": "c-model",
                        "credential_info": {"custom_llm_provider": "hosted_vllm"},
                        "credential_values": {"api_key": "sk-x"},
                        "model_id": model_id,
                    }
                ],
                "models": [
                    {
                        "model_name": "m-bound",
                        "model_info": {"id": model_id, "mode": "chat", "base_model": "b"},
                        "litellm_params": {"model": "hosted_vllm/b"},
                    }
                ],
            }
        )
    )

    plan = reconcile(str(spec), client, dry_run=False)
    assert plan.create_count == 2
    assert "m-bound" in fake.models
    assert "c-model" in fake.credentials
    # second run converges (model_id is write-once, never echoed back)
    plan2 = reconcile(str(spec), client, dry_run=False)
    assert plan2.update_count == 0


def test_guardrail_info_omitted_converges_against_empty_dict(ctx, tmp_path):
    """The live proxy echoes guardrail_info as {} when a guardrail has none,
    while the spec omits it (None). That must not read as perpetual drift."""
    client, fake = ctx
    spec = tmp_path / "spec.yml"
    spec.write_text(
        json.dumps(
            {
                "guardrails": [
                    {
                        "guardrail_name": "g-bare",
                        "litellm_params": {"guardrail": "presidio", "mode": "pre_call"},
                    }
                ]
            }
        )
    )
    reconcile(str(spec), client, dry_run=False)
    # mimic the live read-back: guardrail_info echoed as {} even though
    # the spec never set it
    fake.guardrails["g-bare"]["guardrail_info"] = {}

    plan = reconcile(str(spec), client, dry_run=False)
    gd = [d for d in plan.diffs if d.resource_type == "guardrail"]
    assert all(d.action is Action.NOOP for d in gd), gd


def test_alias_only_team_second_run_is_noop(ctx, tmp_path):
    """Teams without a fixed team_id (alias identity) must create cleanly AND
    not report a phantom default_user_id member on the second run (real proxy
    /team/info never echoes one; found via live permutation testing)."""
    client, fake = ctx
    spec = tmp_path / "spec.yml"
    spec.write_text(
        json.dumps(
            {
                "teams": [
                    {
                        "team_alias": "alias-team",
                        "members_with_roles": [{"user_id": "u1", "role": "admin"}],
                    }
                ]
            }
        )
    )
    reconcile(str(spec), client, dry_run=False)
    plan = reconcile(str(spec), client, dry_run=False)

    team_diffs = [d for d in plan.diffs if d.resource_type == "team"]
    member_diffs = [d for d in plan.diffs if d.resource_type == "team_member"]
    assert all(d.action is Action.NOOP for d in team_diffs), team_diffs
    assert member_diffs == [], member_diffs


def test_team_member_role_change_uses_member_update(ctx, tmp_path):
    """Changing an EXISTING team member's role must call /team/member_update —
    member_add 400s with team_member_already_in_team on the real proxy."""
    client, fake = ctx
    spec = tmp_path / "spec.yml"
    spec.write_text(json.dumps(SPEC))
    reconcile(str(spec), client, dry_run=False)

    # u2's role changes within team-prod
    changed = json.loads(spec.read_text())
    changed["teams"][0]["members_with_roles"] = [
        {"user_id": "u1", "role": "admin"},
        {"user_id": "u2", "role": "admin"},
    ]
    spec.write_text(json.dumps(changed))

    plan = reconcile(str(spec), client, dry_run=False)
    updates = [d for d in plan.diffs if d.action is Action.UPDATE and d.resource_type == "team_member"]
    assert any(d.name == "prod/u2" for d in updates)

    # member_update path was used and the fake stored the new role
    assert fake.team_members[("team-prod", "u2")] == "admin"


def test_policy_drift_recreates_production_version(ctx, tmp_path):
    """Published (production) policies reject PUT on the real proxy
    ("Only draft versions can be updated"); there is no versioning endpoint,
    so a drifted policy must be recreated (delete + re-create) rather than
    updated."""
    client, fake = ctx
    spec = tmp_path / "spec.yml"
    spec.write_text(json.dumps(FULL_SPEC))
    reconcile(str(spec), client, dry_run=False)

    changed = json.loads(spec.read_text())
    changed["policies"][0]["description"] = "updated baseline"
    changed["policies"][0]["guardrails_add"] = ["pii-guard", "toxicity-filter"]
    spec.write_text(json.dumps(changed))

    plan = reconcile(str(spec), client, dry_run=False)
    policy_diffs = [d for d in plan.diffs if d.resource_type == "policy"]
    assert any(d.action is Action.UPDATE for d in policy_diffs)

    # the drifted policy was recreated under the same name with new content
    live = fake._list_policies()
    by_name = {p["policy_name"]: p for p in live}
    assert by_name["global-baseline"]["description"] == "updated baseline"
    assert by_name["global-baseline"]["guardrails_add"] == ["pii-guard", "toxicity-filter"]

    # and the state now converges
    plan2 = reconcile(str(spec), client, dry_run=False)
    policy_diffs2 = [d for d in plan2.diffs if d.resource_type == "policy"]
    assert all(d.action is Action.NOOP for d in policy_diffs2), policy_diffs2


def test_model_tier_validation_rejects_non_enum():
    """model_info.tier is an API enum ('free'|'paid') — an invalid value must
    be caught at spec load (found live: 422 literal_error on PATCH /model/*)."""
    from litellm_as_code.validation import _check_model_tier

    assert _check_model_tier({"model_info": {"tier": "standard"}}) is not None
    assert _check_model_tier({"model_info": {"tier": "paid"}}) is None
    assert _check_model_tier({"model_info": {"tier": "free"}}) is None
    assert _check_model_tier({"model_info": {}}) is None


def test_list_models_empty_db_five_hundred_becomes_empty_list():
    """A fresh proxy with an empty DB makes /model/info return 500
    "LLM Model List not loaded in..." instead of []. list_models() should
    treat that as no models so a brand-new proxy reconciles cleanly."""
    from litellm_as_code.api import LiteLLMClient, ReconcilerError

    client = LiteLLMClient("http://fake:4000", "test-admin-key")
    # _request already converts the HTTP 500 into a ReconcilerError with the
    # proxy's error text inline; list_models() gets that message.
    error_message = (
        "GET /model/info failed: 500 Server Error: Internal Server Error for "
        "url: http://litellm:4000/model/info {\"detail\":{\"error\":\"LLM Model "
        "List not loaded in. Make sure you passed models in your config.yaml or "
        "on the LiteLLM Admin UI. - https://docs.litellm.ai/docs/proxy/configs\"}}"
    )

    def _empty_db_error(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise ReconcilerError(error_message)

    client._request = _empty_db_error  # type: ignore[method-assign]
    assert client.list_models() == []

    # sanity: the underlying error surfaces for other responses
    def _other_error(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise ReconcilerError("GET /model/info failed: boom")

    client._request = _other_error  # type: ignore[method-assign]
    with pytest.raises(ReconcilerError):
        client.list_models()
