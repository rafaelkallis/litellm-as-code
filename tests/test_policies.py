"""Policy reconcile tests against the fake proxy.

Proves: identity is `policy_name`, mutations are `policy_id`-keyed,
config-file-only policies (definition_location="config") are ignored, and a
second run is a no-op.
"""

from __future__ import annotations

import json

import pytest

from litellm_as_code.reconciler import reconcile
from litellm_as_code.types import Action, ReconcilerError

from tests import make_fake_client

SPEC = {
    "policies": [
        {
            "policy_name": "global-baseline",
            "description": "Base guardrails for all requests",
            "guardrails_add": ["pii-guard"],
        },
        {
            "policy_name": "strict-safety",
            "inherit": "global-baseline",
            "description": "Extra safety",
            "guardrails_add": ["toxicity-filter"],
        },
    ]
}


def _write_spec(tmp_path, data):
    path = tmp_path / "spec.yml"
    path.write_text(json.dumps(data))
    return path


def test_first_run_creates_policies(tmp_path):
    client, fake = make_fake_client()
    spec = _write_spec(tmp_path, SPEC)

    plan = reconcile(spec, client, dry_run=False)

    creates = [d for d in plan.diffs if d.action is Action.CREATE]
    assert sorted(d.name for d in creates) == ["global-baseline", "strict-safety"]
    assert len(fake.policies) == 2
    assert fake.policy_ids["global-baseline"]


def test_second_run_is_noop(tmp_path):
    client, fake = make_fake_client()
    spec = _write_spec(tmp_path, SPEC)

    reconcile(spec, client, dry_run=False)
    plan = reconcile(spec, client, dry_run=False)

    policy_diffs = [d for d in plan.diffs if d.resource_type == "policy"]
    assert all(d.action is Action.NOOP for d in policy_diffs)


def test_drift_detects_guardrails_add_change(tmp_path):
    client, fake = make_fake_client()
    spec = _write_spec(tmp_path, SPEC)
    reconcile(spec, client, dry_run=False)

    changed = json.loads(spec.read_text())
    changed["policies"][0]["guardrails_add"].append("pii_header_policy")
    spec.write_text(json.dumps(changed))

    plan = reconcile(spec, client, dry_run=False)
    updates = {d.name: d for d in plan.diffs if d.action is Action.UPDATE}
    assert "global-baseline" in updates
    assert updates["global-baseline"].changes["guardrails_add"][0] == [
        "pii-guard",
        "pii_header_policy",
    ]
    assert fake.policies["global-baseline"]["guardrails_add"] == [
        "pii-guard",
        "pii_header_policy",
    ]


def test_drift_detects_inherit_change(tmp_path):
    client, fake = make_fake_client()
    spec = _write_spec(tmp_path, SPEC)
    reconcile(spec, client, dry_run=False)

    changed = json.loads(spec.read_text())
    changed["policies"][1]["inherit"] = None
    spec.write_text(json.dumps(changed))

    plan = reconcile(spec, client, dry_run=False)
    updates = {d.name: d for d in plan.diffs if d.action is Action.UPDATE}
    assert "strict-safety" in updates
    assert updates["strict-safety"].changes["inherit"] == (None, "global-baseline")
    assert fake.policies["strict-safety"].get("inherit") is None


def test_config_only_policies_are_ignored(tmp_path):
    """A policy with definition_location="config" is startup-only: it must not
    be deleted and must not block creating a matching spec entry."""
    client, fake = make_fake_client()
    fake._list_policies = lambda: [
        {
            "policy_id": "policy_config-1",
            "policy_name": "global-baseline",
            "definition_location": "config",
            "inherit": None,
            "guardrails_add": ["pii-guard"],
            "guardrails_remove": [],
        }
    ]
    spec = _write_spec(tmp_path, SPEC)

    plan = reconcile(spec, client, dry_run=False)

    creates = [d for d in plan.diffs if d.action is Action.CREATE]
    assert sorted(d.name for d in creates) == ["global-baseline", "strict-safety"]


def test_dry_run_does_not_create(tmp_path):
    client, fake = make_fake_client()
    spec = _write_spec(tmp_path, SPEC)

    plan = reconcile(spec, client, dry_run=True)

    assert plan.create_count == 2
    assert len(fake.policies) == 0


def test_draft_policy_drift_updates_in_place(tmp_path):
    """A DRAFT policy accepts PUT, so drift must be reconciled in place —
    no delete, no re-create, no burned version-history entry (issue #10)."""
    client, fake = make_fake_client()
    spec = _write_spec(tmp_path, SPEC)
    reconcile(spec, client, dry_run=False)

    # the live policy is a draft: operators create drafts outside the spec
    fake.policy_drafts.add("global-baseline")
    pid_before = fake.policy_ids["global-baseline"]

    calls: list[str] = []
    real_delete, real_create = client.delete_policy, client.create_policy

    def spy_delete(policy_id):
        calls.append("delete")
        return real_delete(policy_id)

    def spy_create(payload):
        calls.append("create")
        return real_create(payload)

    client.delete_policy = spy_delete  # type: ignore[method-assign]
    client.create_policy = spy_create  # type: ignore[method-assign]

    changed = json.loads(spec.read_text())
    changed["policies"][0]["description"] = "draft-edited baseline"
    spec.write_text(json.dumps(changed))

    plan = reconcile(spec, client, dry_run=False)
    updates = {d.name: d for d in plan.diffs if d.action is Action.UPDATE}
    assert "global-baseline" in updates
    assert updates["global-baseline"].message == "updated in place (draft PUT)"

    # PUT path only: no delete, no re-create, identity (policy_id) preserved
    assert calls == []
    assert fake.policies["global-baseline"]["description"] == "draft-edited baseline"
    assert fake.policy_ids["global-baseline"] == pid_before


def test_failing_recreate_after_delete_surfaces_and_destroys(tmp_path):
    """A publish-locked policy whose PUT is rejected is deleted and re-created;
    if the re-create then fails, the exception must surface loudly (not be
    swallowed) and the destroyed state must be visible (.accepted residual
    non-atomicity, documented in resources/policies.py — issue #10)."""
    client, fake = make_fake_client()
    spec = _write_spec(tmp_path, SPEC)
    reconcile(spec, client, dry_run=False)

    changed = json.loads(spec.read_text())
    changed["policies"][0]["description"] = "will fail to recreate"
    spec.write_text(json.dumps(changed))

    calls: list[str] = []
    real_delete = client.delete_policy

    def failing_create(payload):
        calls.append("create")
        raise ReconcilerError("POST /policies failed: 503 Server Error")

    def spy_delete(policy_id):
        calls.append("delete")
        return real_delete(policy_id)

    client.delete_policy = spy_delete  # type: ignore[method-assign]
    client.create_policy = failing_create  # type: ignore[method-assign]

    with pytest.raises(ReconcilerError, match="POST /policies failed"):
        reconcile(spec, client, dry_run=False)

    # delete happened first, the failing create was attempted, and the policy
    # is gone (divergence worse than drift — surfaced, not hidden)
    assert calls == ["delete", "create"]
    assert "global-baseline" not in fake.policies
    assert "global-baseline" not in fake.policy_ids


def test_dry_run_drift_reports_both_paths(tmp_path):
    """In dry-run the reconciler cannot probe the PUT (it would mutate a
    draft), so the plan message must name both possible apply outcomes."""
    client, fake = make_fake_client()
    spec = _write_spec(tmp_path, SPEC)
    reconcile(spec, client, dry_run=False)

    changed = json.loads(spec.read_text())
    changed["policies"][0]["description"] = "planned change"
    spec.write_text(json.dumps(changed))

    plan = reconcile(spec, client, dry_run=True)
    updates = {d.name: d for d in plan.diffs if d.action is Action.UPDATE}
    assert updates["global-baseline"].message == (
        "update (draft PUT) or recreate (production)"
    )
    assert len(fake.policies) == 2  # nothing mutated
