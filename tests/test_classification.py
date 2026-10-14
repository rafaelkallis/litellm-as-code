"""Kernel units for the generic drift classification (issue #29)."""

from __future__ import annotations

import pytest

from litellm_as_code import classification as cl
from litellm_as_code import log
from litellm_as_code.log import set_quiet_classified
from litellm_as_code.types import Action, Diff


@pytest.fixture(autouse=True)
def _fresh_run():
    cl.reset_run()
    set_quiet_classified(False)
    yield
    cl.reset_run()
    set_quiet_classified(False)


def _diff(action=Action.UPDATE, name="b1", fields=None):
    return Diff("budget", name, action, dict(fields or {}))


FIELDS = ("rpm_limit", "max_budget", "mode")


def _entry(rpm=30, mx=100.0, mode="llm"):
    return {"rpm_limit": rpm, "max_budget": mx, "mode": mode}


def _rediff(entry, row):
    return {
        k: (entry[k], row.get(k))
        for k in FIELDS
        if row.get(k) != entry[k]
    }


def _rows(rows):
    return {r["budget_id"]: r for r in rows if r.get("budget_id")}



# --- registry & per-run classification --------------------------------------


def test_registry_starts_empty():
    """Deliberately empty (issue #29 plan): known classes are capability-
    gated or diff-time; this registry is for unconditionally server-owned
    field classes only."""
    assert cl.VERIFIED_SERVER_OWNED == {}


def test_default_is_writable():
    writable, classified = cl.writable_changes(
        "budget", {"rpm_limit": (30, None), "max_budget": (100.0, 50.0)}
    )
    assert set(writable) == {"rpm_limit", "max_budget"}
    assert classified == {}


def test_declare_server_owned_scopes_to_run():
    cl.declare_server_owned("budget", ["rpm_limit"], "reason-A")
    writable, classified = cl.writable_changes(
        "budget", {"rpm_limit": (30, None), "max_budget": (100.0, 50.0)}
    )
    assert set(writable) == {"max_budget"}
    assert classified == {"rpm_limit": "reason-A"}
    # scoping is per (resource_type, field): another resource type unaffected
    w2, c2 = cl.writable_changes("model", {"rpm_limit": (1, None)})
    assert set(w2) == {"rpm_limit"} and c2 == {}


def test_reset_run_clears_classification():
    cl.declare_server_owned("budget", ["rpm_limit"], "reason-A")
    cl.reset_run()
    assert cl.classified_reason("budget", "rpm_limit") is None


def test_writable_changes_does_not_mutate_input():
    changes = {"rpm_limit": (30, None)}
    cl.declare_server_owned("budget", ["rpm_limit"], "reason-A")
    writable, classified = cl.writable_changes("budget", changes)
    assert writable == {}
    assert classified == {"rpm_limit": "reason-A"}
    assert set(changes) == {"rpm_limit"}


# --- accepted_message & Diff downgrade ---------------------------------------


def test_message_is_machine_greppable():
    cl.declare_server_owned("budget", ["rpm_limit"], "reason-A")
    _w, classified = cl.writable_changes("budget", {"rpm_limit": (30, None)})
    assert cl.accepted_message(classified) == "SERVER_OWNED — rpm_limit: reason-A"


def test_full_downgrade_update_to_noop():
    cl.declare_server_owned("budget", ["rpm_limit"], "reason-A")
    d = _diff(fields={"rpm_limit": (30, None)})
    assert cl.accept_drift(d, {"rpm_limit": "reason-A"}) is True
    assert d.action is Action.NOOP
    assert d.changes == {}
    assert "SERVER_OWNED — rpm_limit: reason-A" in d.message


def test_partial_downgrade_keeps_update_with_writable_changes():
    cl.declare_server_owned("budget", ["rpm_limit"], "reason-A")
    d = _diff(fields={"rpm_limit": (30, None), "max_budget": (100.0, 50.0)})
    assert cl.accept_drift(d, {"rpm_limit": "reason-A"}) is False
    assert d.action is Action.UPDATE
    assert set(d.changes) == {"max_budget"}
    assert "SERVER_OWNED" in d.message


def test_accept_drift_skips_fields_not_on_diff():
    d = _diff(fields={"max_budget": (100.0, 50.0)})
    cl.accept_drift(d, {"rpm_limit": "reason-A"})
    assert d.action is Action.UPDATE
    assert set(d.changes) == {"max_budget"}  # nothing popped


# --- classify_survivors (apply-then-verify) ----------------------------------


def test_survivors_downgraded_to_accepted_noop():
    d = _diff(fields={"rpm_limit": (30, None)})
    rows = [{"budget_id": "b1", "rpm_limit": None, "max_budget": 100.0}]
    downgraded = cl.classify_survivors(
        "budget", rows, [(d, _entry())], index=_rows, rediff=_rediff
    )
    # rpm_limit did not stick (server-owned) -> accepted, and the diff's
    # only change set entry is gone -> UPDATE downgrades to an accepted NOOP.
    assert downgraded == 1
    assert d.action is Action.NOOP
    assert d.changes == {}
    assert "SERVER_OWNED — rpm_limit:" in d.message


def test_survivors_partial_stick_keeps_real_update():
    """Only the UNPERSISTED fields are popped. A field verified stuck stays
    as the real UPDATE the run already applied — the verify never removes
    writable drift, it only accounts for demonstrated non-persistence."""
    d = _diff(fields={"rpm_limit": (30, None), "max_budget": (100.0, 50.0)})
    rows = [{"budget_id": "b1", "rpm_limit": None, "max_budget": 100.0}]
    downgraded = cl.classify_survivors(
        "budget", rows, [(d, _entry())], index=_rows, rediff=_rediff
    )
    assert downgraded == 0  # max_budget remains a real (applied) update
    assert d.action is Action.UPDATE
    assert set(d.changes) == {"max_budget"}
    assert "SERVER_OWNED — rpm_limit:" in d.message


def test_survivors_all_fields_stuck_is_not_touched():
    """Every field persisted -> no survivors -> diff untouched."""
    d = _diff(fields={"rpm_limit": (30, None)})
    rows = [{"budget_id": "b1", "rpm_limit": 30}]
    assert (
        cl.classify_survivors(
            "budget", rows, [(d, _entry())], index=_rows, rediff=_rediff
        )
        == 0
    )
    assert d.action is Action.UPDATE


def test_survivors_race_skip_leaves_diff_untouched():
    """Row vanished between the write and the re-list -> NO classification
    (the verify is impossible); the diff stays exactly as the stage left
    it — the next run decides (today's models precedent)."""
    d = _diff(fields={"rpm_limit": (30, None)})
    downgraded = cl.classify_survivors(
        "budget", [], [(d, _entry())], index=_rows, rediff=_rediff
    )
    assert downgraded == 0
    assert d.action is Action.UPDATE
    assert d.message == ""


def test_diff_unknown_to_the_rows_map_is_race_skipped():
    d = _diff(name="b2", fields={"rpm_limit": (30, None)})
    rows = [{"budget_id": "b1", "rpm_limit": None}]
    downgraded = cl.classify_survivors(
        "budget", rows, [(d, _entry())], index=_rows, rediff=_rediff
    )
    assert downgraded == 0
    assert d.action is Action.UPDATE


# --- notices: once per run, quiet-gated ---------------------------------------


def test_notice_once_per_run(capsys):
    cl.declare_server_owned("budget", ["rpm_limit"], "reason-A")
    for _ in range(3):
        d = _diff(fields={"rpm_limit": (30, None)})
        cl.accept_drift(d, {"rpm_limit": "reason-A"})
    err = capsys.readouterr().err
    assert err.count("SERVER_OWNED") == 1


def test_distinct_reasons_get_distinct_notices(capsys):
    d1 = _diff(fields={"rpm_limit": (30, None)})
    cl.accept_drift(d1, {"rpm_limit": "reason-A"})
    d2 = _diff(fields={"mode": ("llm", "embedding")})
    cl.accept_drift(d2, {"mode": "reason-B"})
    err = capsys.readouterr().err
    assert "reason-A" in err and "reason-B" in err


def test_quiet_suppresses_notice_but_not_the_plan_marker(capsys):
    set_quiet_classified(True)
    d = _diff(fields={"rpm_limit": (30, None)})
    cl.accept_drift(d, {"rpm_limit": "reason-A"})
    out = capsys.readouterr()
    assert "SERVER_OWNED" not in out.err
    assert "SERVER_OWNED" in d.message


def test_warn_channel_not_quiet_gated(capsys):
    """Other warns stay always-on (scripts must keep seeing masked-data
    guidance) — only the classification channel is quiet-gated."""
    set_quiet_classified(True)
    log.warn("litellm-as-code", "some masked credential guidance")
    err = capsys.readouterr().err
    assert "some masked credential guidance" in err
