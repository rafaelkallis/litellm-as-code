"""Credential write-once + value-masking tests against the fake proxy.

Proves the two properties that underpin the stateless credential handling:
1. GET never returns credential_values in plaintext (masked), so no secret
   can ever be diffed against live — values are write-once.
2. A comparable (non-secret) change triggers a PATCH that re-asserts the
   full payload; the server-side value is updated (still masked on read).
"""

from __future__ import annotations

import json

from litellm_as_code.reconciler import reconcile

from tests import make_fake_client

SPEC = {
    "credentials": [
        {
            "credential_name": "c1",
            "credential_info": {"custom_llm_provider": "hosted_vllm"},
            "credential_values": {"api_key": "sk-provider", "api_base": "http://v:8000"},
        }
    ]
}


def _write_spec(tmp_path, data) -> str:
    path = tmp_path / "spec.yml"
    path.write_text(json.dumps(data))
    return str(path)


def test_credential_values_are_never_read_back_plaintext(tmp_path):
    client, fake = make_fake_client()
    spec = _write_spec(tmp_path, SPEC)

    reconcile(spec, client, dry_run=False)

    # The stored credential read back masks every value (like the real API).
    cred = client.get_credential_by_name("c1")
    assert "sk-provider" not in json.dumps(cred["credential_values"])
    assert "http://v:8000" not in json.dumps(cred["credential_values"])
    assert all("***" in str(v) for v in cred["credential_values"].values())


def test_credential_info_drift_patches_and_reasserts_values(tmp_path):
    client, fake = make_fake_client()
    spec_path = _write_spec(tmp_path, SPEC)

    reconcile(spec_path, client, dry_run=False)

    # comparable-field drift (credential_info change) triggers a PATCH that
    # re-asserts the full payload, including credential_values.
    changed = json.loads(json.dumps(SPEC))
    changed["credentials"][0]["credential_info"]["custom_llm_provider"] = "openai"

    plan = reconcile(_write_spec(tmp_path, changed), client, dry_run=False)
    updates = [d for d in plan.diffs if d.action.value == "update"]
    assert len(updates) == 1
    assert updates[0].name == "c1"

    # server-side values were re-asserted with the spec's current value.
    assert fake._stored_credential_values["c1"]["api_key"] == "sk-provider"
