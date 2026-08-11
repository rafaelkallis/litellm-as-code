"""Organization + org member reconcile tests against the fake proxy.

Proves: identity is `organization_id` (with `organization_alias` fallback);
members reconcile via member_add / member_update / member_delete; and a second
run is a no-op.
"""

from __future__ import annotations

import json

import pytest

from litellm_as_code.reconciler import reconcile
from litellm_as_code.types import Action

from tests import make_fake_client

SPEC = {
    "organizations": [
        {
            "organization_id": "org-acme",
            "organization_alias": "acme",
            "members_with_roles": [
                {"user_id": "u1", "role": "org_admin"},
                {"user_id": "u2", "role": "internal_user"},
            ],
        },
        {
            "organization_id": "org-globex",
            "organization_alias": "globex",
        },
    ]
}


def _write_spec(tmp_path, data):
    path = tmp_path / "spec.yml"
    path.write_text(json.dumps(data))
    return path


def test_first_run_creates_orgs_and_members(tmp_path):
    client, fake = make_fake_client()
    spec = _write_spec(tmp_path, SPEC)

    plan = reconcile(spec, client, dry_run=False)

    assert plan.create_count == 4  # 2 orgs + 2 members
    assert len(fake.organizations) == 2
    assert fake.org_members[("org-acme", "u1")] == "org_admin"
    assert fake.org_members[("org-acme", "u2")] == "internal_user"


def test_second_run_is_noop(tmp_path):
    client, fake = make_fake_client()
    spec = _write_spec(tmp_path, SPEC)

    reconcile(spec, client, dry_run=False)
    plan = reconcile(spec, client, dry_run=False)

    org_diffs = [d for d in plan.diffs if d.resource_type == "organization"]
    assert all(d.action is Action.NOOP for d in org_diffs)
    # members converge silently (only create/update of members emits diffs)
    assert plan.update_count == 0


def test_drift_detects_alias_change(tmp_path):
    client, fake = make_fake_client()
    spec = _write_spec(tmp_path, SPEC)
    reconcile(spec, client, dry_run=False)

    changed = json.loads(spec.read_text())
    changed["organizations"][0]["organization_alias"] = "acme-renamed"
    spec.write_text(json.dumps(changed))

    plan = reconcile(spec, client, dry_run=False)
    updates = {d.name: d for d in plan.diffs if d.action is Action.UPDATE}
    assert "acme-renamed" in updates

    # list uses the updated alias
    org = fake.organizations["org-acme"]
    assert org["organization_alias"] == "acme-renamed"


def test_member_role_update_and_removal(tmp_path):
    client, fake = make_fake_client()
    spec = _write_spec(tmp_path, SPEC)
    reconcile(spec, client, dry_run=False)

    changed = json.loads(spec.read_text())
    changed["organizations"][0]["members_with_roles"] = [
        {"user_id": "u2", "role": "org_admin"},
    ]
    spec.write_text(json.dumps(changed))

    plan = reconcile(spec, client, dry_run=False)
    updates = {d.name: d for d in plan.diffs if d.action is Action.UPDATE}

    # role update for u2
    assert "acme/u2" in updates
    assert updates["acme/u2"].changes["role"] == ("internal_user", "org_admin")
    # removal of u1
    assert "acme/u1" in updates

    assert fake.org_members[("org-acme", "u2")] == "org_admin"
    assert ("org-acme", "u1") not in fake.org_members


def test_dry_run_does_not_create(tmp_path):
    client, fake = make_fake_client()
    spec = _write_spec(tmp_path, SPEC)

    plan = reconcile(spec, client, dry_run=True)

    assert plan.create_count == 4  # 2 orgs + 2 member placeholders
    assert len(fake.organizations) == 0
    assert fake.org_members == {}
