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
from litellm_as_code.types import Action, ReconcilerError

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
    plan2 = reconcile(spec, client, dry_run=True)
    updates = {d.name: d for d in plan2.diffs if d.action is Action.UPDATE}
    assert "pii-guard" in updates
    assert updates["pii-guard"].changes["guardrail_info.description"] == (
        "Updated",
        "PII masking",
    )
    # the change fires, but its payload would replace the live map and delete
    # the write-once secrets the spec can't re-declare — the update is
    # DEFERRED (Copilot r2, PR #20): the plan surfaces it, apply REFUSES
    # with a nonzero exit (Copilot r4) instead of silently leaving drift
    assert "deferred" in updates["pii-guard"].message
    with pytest.raises(ReconcilerError):
        reconcile(spec, client, dry_run=False)
    # nothing was patched: the secrets survive untouched
    assert fake.guardrails["pii-guard"]["guardrail_info"]["description"] == (
        "PII masking"
    )
    assert fake.guardrails["pii-guard"]["guardrail_info"]["api_key"] == "abcd***"
    assert fake.guardrails["pii-guard"]["guardrail_info"]["notes"] == "***"

    # re-declaring the secrets (plaintext) in the spec unblocks the update:
    # desired state now includes them, so the replace can no longer destroy
    # write-once material it cannot re-assert
    changed["guardrails"][0]["guardrail_info"] = {
        "description": "Updated",
        "api_key": "sk-live-123",
        "notes": "note-plaintext",
    }
    spec.write_text(json.dumps(changed))
    reconcile(spec, client, dry_run=False)
    info = fake.guardrails["pii-guard"]["guardrail_info"]
    assert info["description"] == "Updated"
    assert info["api_key"] == "sk-live-123"
    assert info["notes"] == "note-plaintext"


def test_dry_run_never_patches(tmp_path):
    """Regression (Copilot r3, PR #20): plan-only runs must not mutate live
    state — the update path records the UPDATE diff but skips the PATCH,
    exactly like every other resource reconciler (dry-run contract)."""
    client, fake = make_fake_client()
    spec = _write_spec(tmp_path, SPEC)
    reconcile(spec, client, dry_run=False)

    changed = json.loads(spec.read_text())
    changed["guardrails"][0]["guardrail_info"]["description"] = "Updated"
    spec.write_text(json.dumps(changed))

    plan = reconcile(spec, client, dry_run=True)
    assert "pii-guard" in {
        d.name for d in plan.diffs if d.action is Action.UPDATE
    }
    # nothing was patched
    assert fake.guardrails["pii-guard"]["guardrail_info"]["description"] == (
        "PII masking"
    )


def test_guardrail_info_null_entry_membership_is_drift(tmp_path):
    """`guardrail_info` is a replacement map: a key explicitly set to `None`
    is a DIFFERENT desired state from a key that is absent — `.get()` on
    both sides would conflate them and hide the drift in either direction
    (Copilot r4, PR #20)."""
    client, fake = make_fake_client()
    spec = _write_spec(tmp_path, SPEC)
    reconcile(spec, client, dry_run=False)
    assert fake.guardrails["pii-guard"]["guardrail_info"] == {
        "description": "PII masking"
    }

    # add an explicitly-None entry: absent -> explicit null is drift
    changed = json.loads(spec.read_text())
    changed["guardrails"][0]["guardrail_info"] = {
        "description": "PII masking",
        "notes": None,
    }
    spec.write_text(json.dumps(changed))
    plan = reconcile(spec, client, dry_run=False)
    updates = {d.name: d for d in plan.diffs if d.action is Action.UPDATE}
    assert updates["pii-guard"].changes["guardrail_info.notes"][0] is None
    assert fake.guardrails["pii-guard"]["guardrail_info"]["notes"] is None

    # ...and dropping it again is drift too: explicit null -> absent
    changed["guardrails"][0]["guardrail_info"] = {"description": "PII masking"}
    spec.write_text(json.dumps(changed))
    plan2 = reconcile(spec, client, dry_run=False)
    updates2 = {d.name: d for d in plan2.diffs if d.action is Action.UPDATE}
    assert updates2["pii-guard"].changes["guardrail_info.notes"][1] is None
    assert "notes" not in fake.guardrails["pii-guard"]["guardrail_info"]


def test_spec_without_guardrail_info_clears_live_map(tmp_path):
    """Regression (Copilot r5, PR #20): a spec that omits guardrail_info
    drift-detects live benign entries (removals), so the PATCH must carry
    the materialized empty map — omitting the field would leave the live
    map uncleaned and re-drift forever."""
    client, fake = make_fake_client()
    spec = _write_spec(tmp_path, SPEC)
    reconcile(spec, client, dry_run=False)

    changed = json.loads(spec.read_text())
    del changed["guardrails"][0]["guardrail_info"]
    spec.write_text(json.dumps(changed))

    reconcile(spec, client, dry_run=False)
    assert fake.guardrails["pii-guard"]["guardrail_info"] == {}

    # converged: no more drift
    plan = reconcile(spec, client, dry_run=False)
    guardrail_diffs = [d for d in plan.diffs if d.resource_type == "guardrail"]
    assert all(d.action is Action.NOOP for d in guardrail_diffs), guardrail_diffs


def test_null_desired_secret_does_not_erase_live_value(tmp_path):
    """A spec entry explicitly set to None on a secret-bearing live key is
    NOT a usable re-assertion — applying it must refuse (deferred), like any
    other loss of write-once material (Copilot r6 theme, PR #20)."""
    client, fake = make_fake_client()
    spec = _write_spec(tmp_path, SPEC)
    reconcile(spec, client, dry_run=False)
    fake.guardrails["pii-guard"]["guardrail_info"] = {
        "description": "PII masking",
        "api_key": "abcd***",
    }

    changed = json.loads(spec.read_text())
    changed["guardrails"][0]["guardrail_info"]["api_key"] = None
    # a benign change co-triggers the PATCH; the null key rides along in the
    # replacement map and would erase the live write-once value
    changed["guardrails"][0]["guardrail_info"]["description"] = "Updated"
    spec.write_text(json.dumps(changed))

    # plan-only: surface as a deferred update
    plan = reconcile(spec, client, dry_run=True)
    updates = {d.name: d for d in plan.diffs if d.action is Action.UPDATE}
    assert "deferred" in updates["pii-guard"].message
    # apply refuses; nothing is patched, the secret survives
    with pytest.raises(ReconcilerError):
        reconcile(spec, client, dry_run=False)
    assert fake.guardrails["pii-guard"]["guardrail_info"]["api_key"] == "abcd***"


def test_nested_secret_is_not_drift_and_is_protected(tmp_path):
    """The same write-once contract applies NESTED (Copilot r9, PR #20): a
    spec exporting `guardrail_info.headers` without the live Authorization
    token must not churn; a benign nested change must not PATCH that token
    away; and re-declaring it unblocks the update."""
    client, fake = make_fake_client()
    spec = _write_spec(tmp_path, SPEC)
    reconcile(spec, client, dry_run=False)
    fake.guardrails["pii-guard"]["guardrail_info"] = {
        "description": "PII masking",
        "headers": {"X-Foo": "bar", "Authorization": "Bearer live-token-1"},
    }

    # exported shape: nested secret stripped; benign siblings intact
    changed = json.loads(spec.read_text())
    changed["guardrails"][0]["guardrail_info"]["headers"] = {"X-Foo": "bar"}
    spec.write_text(json.dumps(changed))
    plan = reconcile(spec, client, dry_run=True)
    guardrail_diffs = [d for d in plan.diffs if d.resource_type == "guardrail"]
    assert all(d.action is Action.NOOP for d in guardrail_diffs), guardrail_diffs

    # ...a benign nested change drifts — and its PATCH would erase the
    # nested bearer token, so apply refuses
    changed = json.loads(spec.read_text())
    changed["guardrails"][0]["guardrail_info"]["headers"]["X-Foo"] = "baz"
    spec.write_text(json.dumps(changed))
    plan2 = reconcile(spec, client, dry_run=True)
    updates = {d.name: d for d in plan2.diffs if d.action is Action.UPDATE}
    assert "deferred" in updates["pii-guard"].message
    with pytest.raises(ReconcilerError):
        reconcile(spec, client, dry_run=False)
    assert fake.guardrails["pii-guard"]["guardrail_info"]["headers"][
        "Authorization"
    ] == "Bearer live-token-1"

    # re-declaring the nested secret (plaintext) unblocks the update
    changed = json.loads(spec.read_text())
    changed["guardrails"][0]["guardrail_info"]["headers"] = {
        "X-Foo": "baz",
        "Authorization": "Bearer fresh-token",
    }
    spec.write_text(json.dumps(changed))
    reconcile(spec, client, dry_run=False)
    assert fake.guardrails["pii-guard"]["guardrail_info"]["headers"] == {
        "X-Foo": "baz",
        "Authorization": "Bearer fresh-token",
    }


def test_list_nested_secret_is_protected(tmp_path):
    """scrub_value also strips secrets from LIST elements, so the
    replacement-safety walk must traverse lists too (Copilot r10, PR #20):
    an exported `rules: [{Authorization: token}]` becomes `rules: [{}]`, and
    a benign co-change must not PATCH that token away."""
    client, fake = make_fake_client()
    spec = _write_spec(tmp_path, SPEC)
    reconcile(spec, client, dry_run=False)
    fake.guardrails["pii-guard"]["guardrail_info"] = {
        "description": "PII masking",
        "rules": [{"Note": "n", "Authorization": "Bearer live-token-1"}],
    }

    # exported shape: rule element scrubbed -> rules: [{}]; no churn
    changed = json.loads(spec.read_text())
    changed["guardrails"][0]["guardrail_info"]["rules"] = [{"Note": "n"}]
    spec.write_text(json.dumps(changed))
    plan = reconcile(spec, client, dry_run=True)
    guardrail_diffs = [d for d in plan.diffs if d.resource_type == "guardrail"]
    assert all(d.action is Action.NOOP for d in guardrail_diffs), guardrail_diffs

    # a benign co-change would replace the map and delete the token: refuse
    changed["guardrails"][0]["guardrail_info"]["description"] = "Updated"
    spec.write_text(json.dumps(changed))
    with pytest.raises(ReconcilerError):
        reconcile(spec, client, dry_run=False)
    assert fake.guardrails["pii-guard"]["guardrail_info"]["rules"][0][
        "Authorization"
    ] == "Bearer live-token-1"

    # re-declaring the token (plaintext) at its index unblocks the update
    changed = json.loads(spec.read_text())
    changed["guardrails"][0]["guardrail_info"] = {
        "description": "Updated",
        "rules": [{"Note": "n", "Authorization": "Bearer fresh-token"}],
    }
    spec.write_text(json.dumps(changed))
    reconcile(spec, client, dry_run=False)
    assert fake.guardrails["pii-guard"]["guardrail_info"]["rules"][0][
        "Authorization"
    ] == "Bearer fresh-token"


def test_list_scalar_mask_keeps_index_alignment(tmp_path):
    """Scrubbing a masked scalar from a list must keep an index placeholder
    (Copilot r11, PR #20): live ["abcd***", "benign"] exports as
    ["<masked>", "benign"]; without the placeholder the exported index 0
    would be mistaken for a usable re-assertion of the live secret and a
    benign co-change could delete it."""
    client, fake = make_fake_client()
    spec = _write_spec(tmp_path, SPEC)
    reconcile(spec, client, dry_run=False)
    fake.guardrails["pii-guard"]["guardrail_info"] = {
        "description": "PII masking",
        "notes_list": ["abcd***", "benign"],
    }

    # re-applying that export is a converged NOOP: the placeholder matches
    # the live mask slot 1:1 (export shape itself is covered in
    # tests/test_exporter.py::test_export_nested_headers_secret_stripped)
    changed = json.loads(spec.read_text())
    changed["guardrails"][0]["guardrail_info"] = {
        "description": "PII masking",
        "notes_list": ["<masked>", "benign"],
    }
    spec.write_text(json.dumps(changed))
    plan0 = reconcile(spec, client, dry_run=True)
    guardrail_diffs = [d for d in plan0.diffs if d.resource_type == "guardrail"]
    assert all(d.action is Action.NOOP for d in guardrail_diffs), guardrail_diffs

    # ...a benign edit of the non-secret element drifts, but the update must
    # NOT be allowed to write over the live masked slot 0
    changed["guardrails"][0]["guardrail_info"]["notes_list"][1] = "changed"
    spec.write_text(json.dumps(changed))
    with pytest.raises(ReconcilerError):
        reconcile(spec, client, dry_run=False)
    assert fake.guardrails["pii-guard"]["guardrail_info"]["notes_list"] == [
        "abcd***",
        "benign",
    ]

    # re-declaring slot 0 (plaintext) unblocks the update
    changed["guardrails"][0]["guardrail_info"]["notes_list"] = [
        "real-secret",
        "changed",
    ]
    spec.write_text(json.dumps(changed))
    reconcile(spec, client, dry_run=False)
    assert fake.guardrails["pii-guard"]["guardrail_info"]["notes_list"] == [
        "real-secret",
        "changed",
    ]


def test_litellm_params_nested_secret_is_protected(tmp_path):
    """LiteLLM shallow-merges litellm_params: top-level params the spec
    omits are preserved, but a re-declared container (the export's scrubbed
    `headers: {X-Foo: bar}`) replaces the whole live nested map and would
    erase the stripped Authorization entry on ANY comparable update (Copilot
    r12, PR #20). The update must defer until the operator re-declares it."""
    client, fake = make_fake_client()
    spec = _write_spec(tmp_path, SPEC)
    reconcile(spec, client, dry_run=False)
    fake.guardrails["pii-guard"]["litellm_params"] = {
        "guardrail": "presidio",
        "mode": "pre_call",
        "headers": {"X-Foo": "bar", "Authorization": "Bearer live-token-1"},
    }

    # exported shape: headers scrubbed top-level; drift on description
    # (guardrail_info per-key) fires — but the PATCH would shallow-merge the
    # scrubbed headers over the live one and erase the token: refuse
    changed = json.loads(spec.read_text())
    changed["guardrails"][0]["litellm_params"]["headers"] = {"X-Foo": "bar"}
    changed["guardrails"][0]["guardrail_info"]["description"] = "Updated"
    spec.write_text(json.dumps(changed))
    plan = reconcile(spec, client, dry_run=True)
    updates = {d.name: d for d in plan.diffs if d.action is Action.UPDATE}
    assert "pii-guard" in updates
    assert "deferred" in updates["pii-guard"].message
    with pytest.raises(ReconcilerError):
        reconcile(spec, client, dry_run=False)
    assert fake.guardrails["pii-guard"]["litellm_params"]["headers"][
        "Authorization"
    ] == "Bearer live-token-1"

    # re-declaring the nested param secret (plaintext) unblocks the update
    changed["guardrails"][0]["litellm_params"]["headers"] = {
        "X-Foo": "bar",
        "Authorization": "Bearer fresh-token",
    }
    spec.write_text(json.dumps(changed))
    reconcile(spec, client, dry_run=False)
    assert fake.guardrails["pii-guard"]["litellm_params"]["headers"] == {
        "X-Foo": "bar",
        "Authorization": "Bearer fresh-token",
    }


def test_nondict_guardrail_info_secret_scrubbed_in_plan(tmp_path):
    """Regression (Copilot r13, PR #20): when the live guardrail_info is
    non-dict (or None) while the spec's map carries a secret-shaped entry,
    the fallback must scrub before recording the change — otherwise the raw
    secret renders into dry-run/log output."""
    client, fake = make_fake_client()
    spec = _write_spec(tmp_path, SPEC)
    reconcile(spec, client, dry_run=False)
    fake.guardrails["pii-guard"]["guardrail_info"] = None

    changed = json.loads(spec.read_text())
    changed["guardrails"][0]["guardrail_info"] = {
        "description": "PII masking",
        "Authorization": "Bearer plaintext-do-not-log",
    }
    spec.write_text(json.dumps(changed))

    plan = reconcile(spec, client, dry_run=True)
    updates = [d for d in plan.diffs if d.action is Action.UPDATE]
    assert updates
    assert "Bearer plaintext-do-not-log" not in str(updates)
    assert "Authorization" not in updates[0].changes  # scrubbed, not leaked


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
