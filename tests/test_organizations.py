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


def _seed_users(fake) -> None:
    """Pre-existing proxy users (adoption flows).

    These specs manage members whose users were created before
    litellm-as-code took over. Seeding keeps these tests about member
    reconciliation; for specs whose users are created in the same run, the
    fake mirrors the pinned proxy's member_add upsert behavior instead
    (issue #12, probed live): an unknown user materializes as a ghost row
    with the server-default role.
    """
    for uid in ("u1", "u2"):
        fake.users[uid] = {
            "user_id": uid,
            "user_alias": uid,
            "user_role": "internal_user",
            "auto_create_key": "false",
        }


def test_first_run_creates_orgs_and_members(tmp_path):
    client, fake = make_fake_client()
    _seed_users(fake)
    spec = _write_spec(tmp_path, SPEC)

    plan = reconcile(spec, client, dry_run=False)

    assert plan.create_count == 4  # 2 orgs + 2 members
    assert len(fake.organizations) == 2
    assert fake.org_members[("org-acme", "u1")] == "org_admin"
    assert fake.org_members[("org-acme", "u2")] == "internal_user"


def test_second_run_is_noop(tmp_path):
    client, fake = make_fake_client()
    _seed_users(fake)
    spec = _write_spec(tmp_path, SPEC)

    reconcile(spec, client, dry_run=False)
    plan = reconcile(spec, client, dry_run=False)

    org_diffs = [d for d in plan.diffs if d.resource_type == "organization"]
    assert all(d.action is Action.NOOP for d in org_diffs)
    # members converge silently (only create/update of members emits diffs)
    assert plan.update_count == 0


def test_drift_detects_alias_change(tmp_path):
    client, fake = make_fake_client()
    _seed_users(fake)
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
    _seed_users(fake)
    spec = _write_spec(tmp_path, SPEC)
    reconcile(spec, client, dry_run=False)

    changed = json.loads(spec.read_text())
    changed["organizations"][0]["members_with_roles"] = [
        {"user_id": "u2", "role": "org_admin"},
    ]
    spec.write_text(json.dumps(changed))

    plan = reconcile(spec, client, dry_run=False)
    updates = {d.name: d for d in plan.diffs if d.action is Action.UPDATE}
    deletes = {d.name: d for d in plan.diffs if d.action is Action.DELETE}

    # role update for u2
    assert "acme/u2" in updates
    assert updates["acme/u2"].changes["role"] == ("internal_user", "org_admin")
    # removal of u1 renders as a DELETE, not an UPDATE({}) (issue #7)
    assert "acme/u1" in deletes
    assert "acme/u1" not in updates
    assert deletes["acme/u1"].changes == {}

    assert fake.org_members[("org-acme", "u2")] == "org_admin"
    assert ("org-acme", "u1") not in fake.org_members


def test_org_member_role_omitted_does_not_churn(tmp_path):
    """A spec org member with an omitted role must converge: the reconciler
    resolves the shared org role default instead of diffing `None` against
    the server's defaulted echo (issue #7) — and a role-less
    /organization/member_update can never fire (the fake asserts on it)."""
    client, fake = make_fake_client()
    _seed_users(fake)
    spec_data = json.loads(json.dumps(SPEC))
    spec_data["organizations"][0]["members_with_roles"] = [{"user_id": "u1"}]
    spec = _write_spec(tmp_path, spec_data)

    plan1 = reconcile(spec, client, dry_run=False)
    updates1 = [d for d in plan1.diffs if d.action is Action.UPDATE]
    assert updates1 == [], f"first run must not churn: {updates1}"
    assert fake.org_members[("org-acme", "u1")] == "internal_user"  # server default

    plan2 = reconcile(spec, client, dry_run=False)
    member_diffs = [
        d for d in plan2.diffs if d.resource_type == "organization_member"
    ]
    assert member_diffs == [], member_diffs


def test_dry_run_does_not_create(tmp_path):
    client, fake = make_fake_client()
    spec = _write_spec(tmp_path, SPEC)

    plan = reconcile(spec, client, dry_run=True)

    assert plan.create_count == 4  # 2 orgs + 2 member placeholders
    assert len(fake.organizations) == 0
    assert fake.org_members == {}


def test_org_member_role_unset_on_read_does_not_churn(tmp_path):
    """Issue #12: a read that leaves user_role unset must compare against the
    resolved server default, not read as perpetual role churn — while an
    explicit spec role still fires member_update (single-direction). The
    pinned proxy always echoes member roles (probed live), so this pins a
    convergence guarantee for the spec-omits-role shape rather than an
    observed drift class."""
    client, fake = make_fake_client()
    _seed_users(fake)
    spec = _write_spec(tmp_path, SPEC)
    reconcile(spec, client, dry_run=False)

    # u2's spec role is internal_user (the org default): unset on read must
    # not read as drift.
    fake.org_members[("org-acme", "u2")] = None
    plan = reconcile(spec, client, dry_run=False)
    member_updates = [
        d
        for d in plan.diffs
        if d.resource_type == "organization_member" and d.action is Action.UPDATE
    ]
    assert member_updates == [], member_updates

    # u1's spec role is explicit (org_admin): that must still diff.
    fake.org_members[("org-acme", "u1")] = None
    plan = reconcile(spec, client, dry_run=True)
    updates = {
        d.name: d
        for d in plan.diffs
        if d.resource_type == "organization_member" and d.action is Action.UPDATE
    }
    assert updates["acme/u1"].changes["role"] == ("internal_user", "org_admin")
