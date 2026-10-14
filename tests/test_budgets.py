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


def test_budget_explicit_zero_converges_and_stays_noop(tmp_path):
    """A spec-set `max_budget: 0` converges the row and stays a NOOP: 0 and
    0.0 compare numerically equal, so the echoed stored zero never churns
    (issue #12 verified live: unset limits read back as None, not 0/0.0)."""
    client, fake = make_fake_client()
    path = tmp_path / "spec.yml"
    path.write_text(
        json.dumps(
            {"budgets": [{"budget_id": "platform-budget", "budget_duration": "30d"}]}
        )
    )
    reconcile(path, client, dry_run=False)
    assert "max_budget" not in fake.budgets["platform-budget"]

    # the spec now intentionally sets max_budget: 0
    path.write_text(
        json.dumps(
            {
                "budgets": [
                    {"budget_id": "platform-budget", "max_budget": 0, "budget_duration": "30d"}
                ]
            }
        )
    )
    plan = reconcile(path, client, dry_run=False)
    updates = [d for d in plan.diffs if d.action is Action.UPDATE]
    assert [d.name for d in updates] == ["platform-budget"]
    assert updates[0].changes["max_budget"] == (0, None)
    assert fake.budgets["platform-budget"]["max_budget"] == 0

    # proxy echoes the stored zero back: converged, never churns
    plan2 = reconcile(path, client, dry_run=False)
    budget_diffs = [d for d in plan2.diffs if d.resource_type == "budget"]
    assert all(d.action is Action.NOOP for d in budget_diffs), budget_diffs


# ==== Synthetic non-persistence (issue #29): generic drift classification ====
#
# The fake normally persists every update_budget payload verbatim. These
# tests wrap `client.update_budget` (unchanged fakes.py) so the proxy
# "refuses to persist" one field (rpm_limit) while accepting the write —
# the same shape real proxies show for, e.g., per-deployment model costs
# on LiteLLM >= 1.102 (the capability-gated case the kernels grew out of).


def _drift_rpm_spec(tmp_path, value):
    data = json.loads(json.dumps(SPEC))
    data["budgets"][1]["rpm_limit"] = value
    return _write_spec(tmp_path, data)


def _never_persists_rpm(client, fake, times=None):
    """Wrap update_budget: every write is accepted but rpm_limit is dropped."""
    orig = client.update_budget
    calls = {"n": 0}

    def wrapper(payload):
        calls["n"] += 1
        if times is None or calls["n"] <= times:
            payload = {k: v for k, v in payload.items() if k != "rpm_limit"}
        return orig(payload)

    client.update_budget = wrapper  # type: ignore[method-assign]
    return calls


def test_synthetic_non_persistence_accepted_on_apply(tmp_path, capsys):
    """Run 1: the drift applies, the un-persisted field is detected and
    accepted (UPDATE downgraded to an accepted NOOP plan line), and the
    classification notice fires exactly once on stderr."""
    client, fake = make_fake_client()
    _never_persists_rpm(client, fake)
    reconcile(_write_spec(tmp_path, SPEC), client, dry_run=False)

    plan = reconcile(_drift_rpm_spec(tmp_path, 2000), client, dry_run=False)
    diff = next(d for d in plan.diffs if d.name == "service-budget")
    assert diff.action is Action.NOOP
    assert diff.changes == {}
    assert "SERVER_OWNED — rpm_limit:" in diff.message
    # the fake row keeps the server-owned old value (the write was dropped)
    assert fake.budgets["service-budget"]["rpm_limit"] == 1000
    # notice: once per run, on stderr
    assert capsys.readouterr().err.count("SERVER_OWNED") == 1


def test_synthetic_non_persistence_second_run_converged(tmp_path, capsys):
    """Run 2 (issue #29 acceptance criterion 2): the drift re-patches dead,
    verify again accepts it, so the plan carries zero pending actions —
    the same shape exit 0 asserts; only the ok-with-reason line remains."""
    client, fake = make_fake_client()
    _never_persists_rpm(client, fake)
    reconcile(_write_spec(tmp_path, SPEC), client, dry_run=False)
    spec_path = _drift_rpm_spec(tmp_path, 2000)

    plan = reconcile(spec_path, client, dry_run=False)
    budget_diffs = [d for d in plan.diffs if d.resource_type == "budget"]
    assert all(d.action is Action.NOOP for d in budget_diffs)  # exit-0 shape
    assert any("SERVER_OWNED" in d.message for d in budget_diffs)
    # a quiet second run suppresses the stderr echo, but the plan marker
    capsys.readouterr()  # flush the non-quiet runs' stderr first
    # stays on the NOOP plan line (stdout contract unchanged)
    plan = reconcile(spec_path, client, dry_run=False, quiet=True)
    err = capsys.readouterr().err
    assert "SERVER_OWNED" not in err
    diff = next(d for d in plan.diffs if d.name == "service-budget")
    assert "SERVER_OWNED" in diff.message
    assert all(d.action is Action.NOOP for d in plan.diffs)


def test_synthetic_self_correction_when_persistence_resumes(tmp_path):
    """Per-run classification is not sticky-wrong: if the proxy STARTS
    persisting the field again, the verify sees the write stick and the
    plan keeps the real UPDATE instead of the acceptance."""
    client, fake = make_fake_client()
    _never_persists_rpm(client, fake, times=1)  # persist again from call 2
    reconcile(_write_spec(tmp_path, SPEC), client, dry_run=False)
    spec_path = _drift_rpm_spec(tmp_path, 2000)

    plan = reconcile(spec_path, client, dry_run=False)  # write dropped
    diff = next(d for d in plan.diffs if d.name == "service-budget")
    assert diff.action is Action.NOOP
    assert "SERVER_OWNED" in diff.message

    plan = reconcile(spec_path, client, dry_run=False)  # write persists now
    diff = next(d for d in plan.diffs if d.name == "service-budget")
    assert diff.action is Action.UPDATE  # not swallowed by the classifier
    assert set(diff.changes) == {"rpm_limit"}
    assert fake.budgets["service-budget"]["rpm_limit"] == 2000

    plan = reconcile(spec_path, client, dry_run=False)  # now converged
    diff = next(d for d in plan.diffs if d.name == "service-budget")
    assert diff.action is Action.NOOP and diff.changes == {}


def test_synthetic_dry_run_keeps_showing_pending_update(tmp_path, capsys):
    """--dry-run cannot verify (nothing is applied), so the un-persisted
    drift keeps showing as a pending update with NO acceptance message —
    the dry-run-as-regression-detector asymmetry (documented in
    classification.py)."""
    client, fake = make_fake_client()
    _never_persists_rpm(client, fake)
    reconcile(_write_spec(tmp_path, SPEC), client, dry_run=False)

    plan = reconcile(_drift_rpm_spec(tmp_path, 2000), client, dry_run=True)
    diff = next(d for d in plan.diffs if d.name == "service-budget")
    assert diff.action is Action.UPDATE
    assert set(diff.changes) == {"rpm_limit"}
    assert diff.message == ""
    assert "SERVER_OWNED" not in capsys.readouterr().err
