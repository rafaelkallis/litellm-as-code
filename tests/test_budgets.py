"""Budget reconcile tests against the fake proxy.

Proves identity is `budget_id`, comparable fields are the budget limits table
(never the server-computed `budget_reset_at`), and that a second run is a no-op
even though the fake (like the real API) injects `budget_reset_at`.
"""

from __future__ import annotations

import json

import pytest

from litellm_as_code.reconciler import reconcile
from litellm_as_code.types import Action

from tests import make_fake_client

SPEC = {
    "budgets": [
        {
            "budget_id": "platform-budget",
            "max_budget": 100.0,
            "budget_duration": "30d",
        },
        {
            "budget_id": "service-budget",
            "max_budget": 25.0,
            "soft_budget": 20.0,
            "tpm_limit": 100000,
            "rpm_limit": 1000,
            "budget_duration": "30d",
        },
    ]
}


def _write_spec(tmp_path, data):
    path = tmp_path / "spec.yml"
    path.write_text(json.dumps(data))
    return path


def test_first_run_creates_budgets(tmp_path):
    client, fake = make_fake_client()
    spec = _write_spec(tmp_path, SPEC)

    plan = reconcile(spec, client, dry_run=False)

    creates = [d for d in plan.diffs if d.action is Action.CREATE]
    assert [d.name for d in creates] == ["platform-budget", "service-budget"]
    assert len(fake.budgets) == 2
    # real API injects a server-computed reset time on read
    assert "budget_reset_at" in fake.budgets["platform-budget"]


def test_second_run_is_noop_despite_server_injected_reset_at(tmp_path):
    client, fake = make_fake_client()
    spec = _write_spec(tmp_path, SPEC)

    reconcile(spec, client, dry_run=False)
    plan = reconcile(spec, client, dry_run=False)

    budget_diffs = [d for d in plan.diffs if d.resource_type == "budget"]
    assert all(d.action is Action.NOOP for d in budget_diffs)
    assert fake.budgets["platform-budget"]["max_budget"] == 100.0


def test_drift_detects_max_budget_change(tmp_path):
    client, fake = make_fake_client()
    spec = _write_spec(tmp_path, SPEC)
    reconcile(spec, client, dry_run=False)

    changed = json.loads(spec.read_text())
    changed["budgets"][0]["max_budget"] = 200.0
    spec.write_text(json.dumps(changed))

    plan = reconcile(spec, client, dry_run=False)
    updates = {d.name: d for d in plan.diffs if d.action is Action.UPDATE}
    assert "platform-budget" in updates
    assert updates["platform-budget"].changes["max_budget"] == (200.0, 100.0)
    assert fake.budgets["platform-budget"]["max_budget"] == 200.0


def test_dry_run_does_not_create(tmp_path):
    client, fake = make_fake_client()
    spec = _write_spec(tmp_path, SPEC)

    plan = reconcile(spec, client, dry_run=True)

    assert plan.create_count == 2
    assert len(fake.budgets) == 0
