"""End-to-end reconciler tests against the fake proxy.

Mirrors `litellm_as_code/` 1:1 like the ecosystem's test layout, but all
mock-only (no real LiteLLM calls).
"""

from __future__ import annotations

import json

import pytest

from litellm_as_code.reconciler import reconcile
from litellm_as_code.state import StateStore
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


@pytest.fixture
def ctx(tmp_path):
    client, fake = make_fake_client()
    store = StateStore(tmp_path / "state.json")
    return client, fake, store


def test_first_run_creates_everything(ctx, tmp_path):
    client, fake, store = ctx
    spec = tmp_path / "spec.yml"
    spec.write_text(json.dumps(SPEC))

    state = store.load()
    plan = reconcile(str(spec), client, state, dry_run=False)
    store.save(state)  # CLI run() does this; unit test mirrors it

    assert plan.create_count == 8  # 2 users + team + 2 members + key + cred + model
    assert plan.update_count == 0
    assert plan.noop_count == 0
    # state file now knows the key/credential secrets
    assert "k1" in store.load().keys
    assert store.load().credentials["c1"]["api_key"] == "sk-provider"


def test_second_run_is_noop(ctx, tmp_path):
    client, fake, store = ctx
    spec = tmp_path / "spec.yml"
    spec.write_text(json.dumps(SPEC))

    state = store.load()
    reconcile(str(spec), client, state, dry_run=False)
    store.save(state)
    state = store.load()
    plan = reconcile(str(spec), client, state, dry_run=False)
    store.save(state)

    assert plan.create_count == 0
    assert plan.update_count == 0
    # Members converge silently (no-op diffs are only emitted for create/update
    # of members); so: 2 users + 1 team + 1 key + 1 cred + 1 model = 6 no-ops.
    assert plan.noop_count == 6

def test_dry_run_does_not_mutate(ctx, tmp_path):
    client, fake, store = ctx
    spec = tmp_path / "spec.yml"
    spec.write_text(json.dumps(SPEC))

    state = store.load()
    plan = reconcile(str(spec), client, state, dry_run=True)

    # In dry-run the team doesn't exist, so its members fold into a single
    # "team/* members" placeholder diff -> 7 create diffs (not 8 per-member).
    assert plan.create_count == 7
    assert len(fake.users) == 0
    assert len(fake.teams) == 0
    assert len(fake.keys) == 0
    assert len(fake.credentials) == 0
    assert len(fake.models) == 0
    assert state.keys == {}  # in-memory state untouched in dry-run


def test_drift_detects_role_change(ctx, tmp_path):
    client, fake, store = ctx
    spec = tmp_path / "spec.yml"
    spec.write_text(json.dumps(SPEC))
    state = store.load()
    reconcile(str(spec), client, state, dry_run=False)
    store.save(state)

    # user u2 role changes
    changed = json.loads(spec.read_text())
    changed["users"][1]["user_role"] = "proxy_admin"
    spec.write_text(json.dumps(changed))

    state = store.load()
    plan = reconcile(str(spec), client, state, dry_run=False)
    store.save(state)
    updates = {d.name: d for d in plan.diffs if d.action is Action.UPDATE}
    assert "worker" in updates
    assert "user_role" in updates["worker"].changes


def test_key_is_not_rotated_on_update(ctx, tmp_path):
    client, fake, store = ctx
    spec = tmp_path / "spec.yml"
    spec.write_text(json.dumps(SPEC))
    state = store.load()
    reconcile(str(spec), client, state, dry_run=False)
    store.save(state)

    original_key = fake.keys["k1"]["key"]

    # change a non-secret key field (e.g. add models), keep same key value
    changed = json.loads(spec.read_text())
    changed["virtual_keys"][0]["models"] = ["org/chat"]
    spec.write_text(json.dumps(changed))

    state = store.load()
    reconcile(str(spec), client, state, dry_run=False)
    store.save(state)
    assert fake.keys["k1"]["key"] == original_key  # no rotation


def test_credential_secret_drift_triggers_patch(ctx, tmp_path):
    client, fake, store = ctx
    spec = tmp_path / "spec.yml"
    spec.write_text(json.dumps(SPEC))
    state = store.load()
    reconcile(str(spec), client, state, dry_run=False)
    store.save(state)

    changed = json.loads(spec.read_text())
    changed["credentials"][0]["credential_values"]["api_key"] = "sk-rotated"
    spec.write_text(json.dumps(changed))

    state = store.load()
    plan = reconcile(str(spec), client, state, dry_run=False)
    store.save(state)
    updates = [d for d in plan.diffs if d.action is Action.UPDATE]
    assert any(d.name == "c1" for d in updates)
    assert store.load().credentials["c1"]["api_key"] == "sk-rotated"


def test_team_member_role_update_and_removal(ctx, tmp_path):
    client, fake, store = ctx
    spec = tmp_path / "spec.yml"
    spec.write_text(json.dumps(SPEC))
    state = store.load()
    reconcile(str(spec), client, state, dry_run=False)

    # change u2's role and drop u1 from the team
    changed = json.loads(spec.read_text())
    changed["teams"][0]["members_with_roles"] = [
        {"user_id": "u2", "role": "admin"},
    ]
    spec.write_text(json.dumps(changed))

    plan = reconcile(str(spec), client, state, dry_run=False)
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
    client, fake, store = ctx
    spec = tmp_path / "spec.yml"
    spec.write_text(json.dumps(SPEC))
    state = store.load()

    plan1 = reconcile(str(spec), client, state, dry_run=False)
    assert any(d.name == "org/chat" and d.action is Action.CREATE for d in plan1.diffs)

    plan2 = reconcile(str(spec), client, state, dry_run=False)
    model_diffs = [d for d in plan2.diffs if d.resource_type == "model"]
    assert all(d.action is Action.NOOP for d in model_diffs)

    # the fake stored cost per-token, exactly like the real proxy
    lp = fake.models["org/chat"]["litellm_params"]
    assert lp["input_cost_per_token"] == 3.0 / 1_000_000.0
