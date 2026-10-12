"""The reconciler stages run AND render in the fixed documented order.

AGENTS.md §5: budgets -> models -> credentials -> organizations -> users ->
org members -> teams (+ members) -> keys -> guardrails -> policies.
`reconciler._stage_diffs` single-sources that order; these pins make a
re-ordering that breaks convergence fail loudly in the mock gate (e.g. the
credential-404s-without-model and users-before-members hand-offs).
"""

from __future__ import annotations

import json

from litellm_as_code.reconciler import reconcile

from tests import make_fake_client

# Exact emission order — mirrors reconciler._stage_diffs. Team members are
# reported under the Teams banner (plan rendering parity, no own banner).
EXPECTED_STAGES = (
    "Budgets",
    "Models",
    "Credentials",
    "Organizations",
    "Users",
    "Organization members",
    "Teams",
    "Virtual keys",
    "Guardrails",
    "Policies",
)


def test_stage_banners_emit_in_the_fixed_order(tmp_path, capsys):
    """Every stage banner appears exactly once, in the documented order."""
    client, _fake = make_fake_client()
    spec = tmp_path / "spec.yml"
    spec.write_text(
        json.dumps(
            {
                "budgets": [],
                "models": [],
                "credentials": [],
                "organizations": [],
                "users": [],
                "teams": [],
                "virtual_keys": [],
                "guardrails": [],
                "policies": [],
            }
        )
    )

    plan = reconcile(str(spec), client, dry_run=True)
    err = capsys.readouterr().err

    # banner(title) writes "\n<title>\n<dash line>" to stderr; find each
    # block's position and assert strictly increasing, unique occurrences.
    positions = [
        err.find(f"\n{title}\n{'-' * len(title)}\n") for title in EXPECTED_STAGES
    ]
    assert all(pos >= 0 for pos in positions), dict(zip(EXPECTED_STAGES, positions))
    assert positions == sorted(positions)
    assert len(set(positions)) == len(positions)  # no duplicate banners

    # `reconcile()` stops at the plan (only `run()` renders it); an empty
    # spec produces zero diffs and no stage emits anything.
    assert plan.diffs == []


def test_members_attach_to_rows_the_users_stage_owns(tmp_path, capsys):
    """Pin the stage hand-off: a spec declaring a user + an org membership
    must create the user row in the Users stage BEFORE adding the member in
    the Organization members stage (users-first, issue #12)."""
    client, fake = make_fake_client()

    order: list[str] = []
    orig_create_user = client.create_user
    orig_add_members = client.add_organization_members
    client.create_user = (  # type: ignore[method-assign]
        lambda *a, **k: (order.append("create_user"), orig_create_user(*a, **k))[1]
    )
    client.add_organization_members = (  # type: ignore[method-assign]
        lambda *a, **k: (
            order.append("add_org_member"), orig_add_members(*a, **k)
        )[1]
    )

    spec = tmp_path / "spec.yml"
    spec.write_text(
        json.dumps(
            {
                "organizations": [
                    {
                        "organization_id": "org-1",
                        "organization_alias": "acme",
                        "members_with_roles": [{"user_id": "u1", "role": "org_admin"}],
                    }
                ],
                "users": [{"user_id": "u1", "user_alias": "u1"}],
            }
        )
    )
    reconcile(str(spec), client, dry_run=False)

    assert order == ["create_user", "add_org_member"]
