"""Tolerance for the LiteLLM Enterprise gate on /organization endpoints.

Since LiteLLM v1.102.0 the entire /organization router is 403-gated behind an
Enterprise license; on unlicensed proxies every /organization/* call answers
HTTP 403 with a body like "Organizations are only available for LiteLLM
Enterprise users" (embedded verbatim in the ReconcilerError text that
LiteLLMClient._request raises). Proves: org-free specs degrade gracefully, a
declared org fails with an actionable message, and anything that is not
unambiguously the gate (missing marker, 5xx) propagates unchanged.
"""

from __future__ import annotations

import json

import pytest

from litellm_as_code.reconciler import reconcile
from litellm_as_code.types import ReconcilerError

from tests import make_fake_client

# Exact shape LiteLLMClient._request produces for the gate: "{method} {path}
# failed: {http error} {response body}".  The response body is normalized to
# single spaces and truncated to 500 chars, so reconstruct the same format.
GATE_TEXT = (
    "GET /organization/list failed: 403 Client Error Forbidden for url: "
    "http://proxy:4000/organization/list Organizations are only available for "
    "LiteLLM Enterprise users"
)

SPEC_WITH_ORG = {
    "organizations": [
        {"organization_id": "org-acme", "organization_alias": "acme"},
    ]
}


def _write_spec(tmp_path, data):
    path = tmp_path / "spec.yml"
    path.write_text(json.dumps(data))
    return path


def _gate_client(error_text: str):
    """Fake client whose list_organizations raises the given error text."""
    client, fake = make_fake_client()

    def _raise_gate():
        raise ReconcilerError(error_text)

    client.list_organizations = _raise_gate  # type: ignore[method-assign]
    return client, fake


def test_org_free_spec_tolerates_gate(tmp_path):
    """Gate 403 with no organizations declared: reconcile completes cleanly."""
    client, fake = _gate_client(GATE_TEXT)
    spec = _write_spec(tmp_path, {})

    plan = reconcile(spec, client, dry_run=False)

    org_diffs = [d for d in plan.diffs if d.resource_type == "organization"]
    assert org_diffs == []
    # The org-members stage must not have re-hit the gated endpoint.
    assert fake.organizations == {}
    assert plan.create_count == 0 and plan.update_count == 0


def test_org_free_spec_gate_degradation_warns(tmp_path, capsys):
    """The degradation path records a stderr warning naming the license gap."""
    client, _fake = _gate_client(GATE_TEXT)
    spec = _write_spec(tmp_path, {})

    plan = reconcile(spec, client, dry_run=False)

    err = capsys.readouterr().err
    assert "[warn]" in err
    assert "organization" in err.lower()
    assert "license" in err.lower()
    assert plan.create_count == 0


def test_spec_with_organizations_fails_actionably(tmp_path):
    """Gate 403 with organizations declared: non-ambiguous operator error."""
    client, _fake = _gate_client(GATE_TEXT)
    spec = _write_spec(tmp_path, SPEC_WITH_ORG)

    with pytest.raises(ReconcilerError) as excinfo:
        reconcile(spec, client, dry_run=False)

    msg = str(excinfo.value)
    assert "1 organization(s)" in msg
    assert "spec" in msg.lower()
    assert "license" in msg.lower()


def test_403_without_enterprise_marker_reraises(tmp_path):
    """A plain 403 (no 'enterprise' marker) is NOT the gate: it re-raises."""
    client, _fake = _gate_client(
        "GET /organization/list failed: 403 Client Error unauthorized"
    )
    spec = _write_spec(tmp_path, {})

    with pytest.raises(ReconcilerError) as excinfo:
        reconcile(spec, client, dry_run=False)

    # Propagated unchanged — the actionable gate message is not grafted on.
    assert "spec declares" not in str(excinfo.value)
    assert "unauthorized" in str(excinfo.value)


def test_server_error_reraises(tmp_path):
    """A 5xx on /organization/list is never swallowed as the gate."""
    client, _fake = _gate_client(
        "GET /organization/list failed: 500 Server Error: Internal Server Error"
    )
    spec = _write_spec(tmp_path, SPEC_WITH_ORG)

    with pytest.raises(ReconcilerError) as excinfo:
        reconcile(spec, client, dry_run=False)

    assert "500" in str(excinfo.value)
    assert "spec declares" not in str(excinfo.value)


def test_export_tolerates_gate_and_exports_orgless(capsys):
    """export degrades like reconcile: gate 403 -> organizations-less spec +
    a warning instead of failing outright."""
    from litellm_as_code.exporter import build_spec

    client, _fake = _gate_client(GATE_TEXT)
    spec = build_spec(client)
    assert spec.get("organizations", []) == []
    assert "organization export skipped" in capsys.readouterr().err


def test_export_re_raises_non_gate_org_errors():
    """A 5xx on /organization/list fails export loudly (never 'the gate')."""
    from litellm_as_code.exporter import build_spec

    client, _fake = _gate_client(
        "GET /organization/list failed: 500 Server Error: Internal Server Error"
    )
    with pytest.raises(ReconcilerError):
        build_spec(client)
