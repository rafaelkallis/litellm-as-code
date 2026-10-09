"""Guardrail reconcile tests against the fake proxy.

Proves the two important guardrail properties:

1. Config-file-only entries (`guardrail_id == None`) are NOT drift targets —
   they are startup configuration and are never diffed or deleted.
2. `litellm_params` is write-once: it is re-asserted only when a comparable
   change (guardrail_name / guardrail_info) triggers a PATCH. Like
   `credential_values`, the API masks secret fields on read.
"""

from __future__ import annotations

import json

import pytest

from litellm_as_code.reconciler import reconcile
from litellm_as_code.types import Action

from tests import make_fake_client

SPEC = {
    "guardrails": [
        {
            "guardrail_name": "pii-guard",
            "litellm_params": {"guardrail": "presidio", "mode": "pre_call"},
            "guardrail_info": {"description": "PII masking"},
        }
    ]
}


def _write_spec(tmp_path, data):
    path = tmp_path / "spec.yml"
    path.write_text(json.dumps(data))
    return path


def test_first_run_creates_guardrail(tmp_path):
    client, fake = make_fake_client()
    spec = _write_spec(tmp_path, SPEC)

    plan = reconcile(spec, client, dry_run=False)

    creates = [d for d in plan.diffs if d.action is Action.CREATE]
    assert [d.name for d in creates] == ["pii-guard"]
    assert len(fake.guardrails) == 1
    assert fake.guardrail_ids["pii-guard"]


def test_second_run_is_noop(tmp_path):
    client, fake = make_fake_client()
    spec = _write_spec(tmp_path, SPEC)

    reconcile(spec, client, dry_run=False)
    plan = reconcile(spec, client, dry_run=False)

    guardrail_diffs = [d for d in plan.diffs if d.resource_type == "guardrail"]
    assert all(d.action is Action.NOOP for d in guardrail_diffs)


def test_guardrail_info_drift_patches(tmp_path):
    client, fake = make_fake_client()
    spec = _write_spec(tmp_path, SPEC)
    reconcile(spec, client, dry_run=False)

    changed = json.loads(spec.read_text())
    changed["guardrails"][0]["guardrail_info"]["description"] = "Updated"
    spec.write_text(json.dumps(changed))

    plan = reconcile(spec, client, dry_run=False)
    updates = {d.name: d for d in plan.diffs if d.action is Action.UPDATE}
    assert "pii-guard" in updates
    # guardrail_info is diffed per key (non-secret subset only)
    assert updates["pii-guard"].changes["guardrail_info.description"] == (
        "Updated",
        "PII masking",
    )
    assert fake.guardrails["pii-guard"]["guardrail_info"]["description"] == "Updated"


def test_guardrail_info_masked_entry_is_not_drift(tmp_path):
    """A live guardrail_info still holding the masked secret entry must NOT
    churn against the spec's stripped (benign-only) subset: the secret subset
    is write-once and carries no diff signal (issue #11, Copilot r1 on PR
    #20 — without this, the exported spec would PATCH forever, either
    deleting the credential or drifting perpetually)."""
    client, fake = make_fake_client()
    spec = _write_spec(tmp_path, SPEC)
    reconcile(spec, client, dry_run=False)

    # emulate the export: the spec carries only the benign subset; live keeps
    # the masked entry the exporter stripped
    changed = json.loads(spec.read_text())
    changed["guardrails"][0]["guardrail_info"] = {"description": "PII masking"}
    spec.write_text(json.dumps(changed))
    fake.guardrails["pii-guard"]["guardrail_info"] = {
        "description": "PII masking",
        "api_key": "abcd***",
        "notes": "***",  # masked-value shape on a benign key: also skipped
    }

    plan = reconcile(spec, client, dry_run=False)
    guardrail_diffs = [d for d in plan.diffs if d.resource_type == "guardrail"]
    assert all(d.action is Action.NOOP for d in guardrail_diffs), guardrail_diffs
    # the masked entry survives — not destroyed by a churn PATCH
    assert fake.guardrails["pii-guard"]["guardrail_info"]["api_key"] == "abcd***"

    # ...but a NON-secret, benign change on either side still drifts
    changed["guardrails"][0]["guardrail_info"]["description"] = "Updated"
    spec.write_text(json.dumps(changed))
    plan2 = reconcile(spec, client, dry_run=False)
    updates = {d.name: d for d in plan2.diffs if d.action is Action.UPDATE}
    assert "pii-guard" in updates
    assert updates["pii-guard"].changes["guardrail_info.description"] == (
        "Updated",
        "PII masking",
    )


def test_litellm_params_reasserted_on_patch(tmp_path):
    """When a comparable change fires a PATCH, litellm_params is re-asserted
    (like credential_values): the update payload carries the full params and
    the server persists them."""
    client, fake = make_fake_client()
    spec = _write_spec(tmp_path, SPEC)
    reconcile(spec, client, dry_run=False)

    changed = json.loads(spec.read_text())
    changed["guardrails"][0]["guardrail_info"]["description"] = "Updated"
    changed["guardrails"][0]["litellm_params"]["mode"] = "post_call"
    spec.write_text(json.dumps(changed))

    reconcile(spec, client, dry_run=False)

    assert fake.guardrails["pii-guard"]["litellm_params"]["mode"] == "post_call"


def test_config_only_guardrails_are_ignored(tmp_path):
    """A guardrail with guardrail_id=None (loaded from the proxy's config.yaml)
    is startup-only: it must not be deleted and must not block creating a
    matching spec entry."""
    client, fake = make_fake_client()
    # graft a config-loaded entry into the fake list (no guardrail_id)
    fake._list_guardrails = lambda: [
        {
            "guardrail_id": None,
            "guardrail_name": "pii-guard",
            "litellm_params": {"guardrail": "presidio", "mode": "pre_call"},
            "guardrail_info": {"description": "PII masking"},
            "guardrail_definition_location": "config",
        }
    ]
    spec = _write_spec(tmp_path, SPEC)

    plan = reconcile(spec, client, dry_run=False)

    # config-only entry is not a match for DB identity, so the spec creates one
    creates = [d for d in plan.diffs if d.action is Action.CREATE]
    assert [d.name for d in creates] == ["pii-guard"]
