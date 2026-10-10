"""Model cost reconcile tests: per-deployment cost acceptance (SERVER_DERIVED_PRICING).

The pinned v1.97.0 proxy persists per-deployment costs (they round-trip on
read); LiteLLM >= 1.102 does NOT persist them (/model/info reads them back as
null even immediately after create/patch — upstream 76cb0fec1 +
42541a923/5d3d99eb9). Without acceptance the plan never converges: perpetual
"model X would be updated (input_cost_per_token: ... -> None)". The fake's
`drop_model_costs` knob mirrors the >= 1.102 generation.
"""

from __future__ import annotations

import json

from litellm_as_code.reconciler import reconcile
from litellm_as_code.types import Action

from tests import make_fake_client


def _spec(model: dict) -> dict:
    return {"models": [model]}


def _write_spec(tmp_path, data) -> object:
    path = tmp_path / "spec.yml"
    path.write_text(json.dumps(data))
    return path


def _model_diff(plan):
    diffs = [d for d in plan.diffs if d.resource_type == "model"]
    assert len(diffs) == 1
    return diffs[0]


def test_persisting_proxy_cost_change_converges(tmp_path):
    """v1.97 semantics (fake persists costs): apply -> next run is a plain NOOP."""
    client, fake = make_fake_client()
    spec_path = _write_spec(
        tmp_path,
        _spec(
            {
                # fake conventions mirror test_reconciler.py: model_info.id is
                # pre-seeded so PATCH /model/{id}/update matches the row
                "model_name": "openai/gpt-4o",
                "model_info": {"id": "m1", "input_cost_per_million_tokens": 2.0},
            }
        ),
    )
    reconcile(spec_path, client, dry_run=False)  # create (costs persisted)
    assert "_per_token" not in json.dumps(list(fake.models))
    spec2 = tmp_path / "spec2.yml"
    spec2.write_text(
        json.dumps(
            _spec(
                {
                    "model_name": "openai/gpt-4o",
                    "model_info": {"id": "m1", "input_cost_per_million_tokens": 3.0},
                }
            )
        )
    )
    plan = reconcile(spec2, client, dry_run=False)
    diff = _model_diff(plan)
    assert diff.action is Action.UPDATE
    assert diff.changes["input_cost_per_token"] == (3e-06, 2e-06)
    # patch persisted: a later reconcile is a *plain* NOOP (no acceptance msg)
    plan = reconcile(spec2, client, dry_run=False)
    diff = _model_diff(plan)
    assert diff.action is Action.NOOP
    assert diff.message == ""
    assert str(diff).endswith(" ok")


def test_cost_only_drift_accepted_on_non_persisting_proxy(tmp_path):
    """>= 1.102 semantics: the dead PATCH stays but the Diff is accepted as a
    NOOP carrying the SERVER_DERIVED_PRICING message — on every run, so the
    plan converges instead of wedging at "would be updated" forever."""
    client, fake = make_fake_client()
    fake.drop_model_costs = True  # mirrors LiteLLM >= 1.102
    spec_path = _write_spec(
        tmp_path, _spec({"model_name": "openai/gpt-4o", "model_info": {"id": "m2"}})
    )
    reconcile(spec_path, client, dry_run=False)  # create, no costs in spec

    # spec now declares a cost: pure cost-only drift
    spec_path.write_text(
        json.dumps(
            _spec(
                {
                    "model_name": "openai/gpt-4o",
                    "model_info": {"id": "m2", "input_cost_per_million_tokens": 2.0},
                }
            )
        )
    )
    patch_calls = []
    orig_patch = client.patch_model
    client.patch_model = (  # type: ignore[method-assign]
        lambda *a, **k: (patch_calls.append(a), orig_patch(*a, **k))[1]
    )

    plan = reconcile(spec_path, client, dry_run=False)
    diff = _model_diff(plan)
    assert diff.action is Action.NOOP  # accepted, not "would be updated"
    # Diff.__str__ renders NOOP as "ok" (types.py); the message lives on the
    # Diff itself and is echoed to stderr by the one-per-run warn.
    assert "SERVER_DERIVED_PRICING" in diff.message
    assert "cost" in diff.message
    rendered = str(diff)
    assert "would be updated" not in rendered
    assert rendered.endswith("ok")
    # the dead PATCH is still sent (harmless on both generations)
    assert len(patch_calls) == 1

    # second run: still NOOP + message (no perpetual drift)
    plan = reconcile(spec_path, client, dry_run=False)
    diff = _model_diff(plan)
    assert diff.action is Action.NOOP
    assert "SERVER_DERIVED_PRICING" in diff.message
    assert "would be updated" not in str(diff)
    assert len(patch_calls) == 2


def test_mixed_drift_never_swallowed_by_cost_acceptance(tmp_path):
    """A non-cost change (mode) keeps the Diff an UPDATE even though the cost
    part of the same PATCH does not stick on a >= 1.102 proxy."""
    client, fake = make_fake_client()
    fake.drop_model_costs = True
    spec_path = _write_spec(
        tmp_path,
        _spec(
            {
                "model_name": "openai/gpt-4o",
                "model_info": {"id": "m3", "mode": "llm"},
            }
        ),
    )
    reconcile(spec_path, client, dry_run=False)

    spec_path.write_text(
        json.dumps(
            _spec(
                {
                    "model_name": "openai/gpt-4o",
                    "model_info": {
                        "id": "m3",
                        "mode": "embedding",
                        "input_cost_per_million_tokens": 2.0,
                    },
                }
            )
        )
    )
    plan = reconcile(spec_path, client, dry_run=False)
    diff = _model_diff(plan)
    assert diff.action is Action.UPDATE
    assert diff.message == ""
    assert "would be updated" in str(diff)
    assert set(diff.changes) == {"mode", "input_cost_per_token"}
    # the non-cost change WAS applied to the fake
    assert diff.changes["mode"] == ("embedding", "llm")


def test_dry_run_keeps_cost_update_and_no_verification(tmp_path):
    """Dry-run cannot PATCH so there is nothing to verify: the plan still
    shows the cost update (documented asymmetry), and patch_model is never
    called."""
    client, fake = make_fake_client()
    fake.drop_model_costs = True
    spec_path = _write_spec(
        tmp_path, _spec({"model_name": "openai/gpt-4o", "model_info": {"id": "m4"}})
    )
    reconcile(spec_path, client, dry_run=False)

    spec_path.write_text(
        json.dumps(
            _spec(
                {
                    "model_name": "openai/gpt-4o",
                    "model_info": {"id": "m4", "input_cost_per_million_tokens": 2.0},
                }
            )
        )
    )
    patch_calls = []
    orig_patch = client.patch_model
    client.patch_model = (  # type: ignore[method-assign]
        lambda *a, **k: (patch_calls.append(a), orig_patch(*a, **k))[1]
    )

    plan = reconcile(spec_path, client, dry_run=True)
    diff = _model_diff(plan)
    assert diff.action is Action.UPDATE
    assert diff.message == ""
    assert "would be updated" in str(diff)
    assert "SERVER_DERIVED_PRICING" not in str(diff)
    assert patch_calls == []


def test_acceptance_handled_per_model(tmp_path):
    """Two models patched for cost-only drift are accepted independently: each
    Diff is downgraded on its own, and a model with no drift is untouched."""
    client, fake = make_fake_client()
    fake.drop_model_costs = True
    spec_path = _write_spec(
        tmp_path,
        {
            "models": [
                {
                    "model_name": "a/model",
                    "model_info": {"id": "ma", "input_cost_per_million_tokens": 2.0},
                },
                {
                    "model_name": "b/model",
                    "model_info": {"id": "mb", "input_cost_per_million_tokens": 5.0},
                },
            ],
        },
    )
    reconcile(spec_path, client, dry_run=False)  # creates; fake persists costs on create

    model_b = fake.models["b/model"]
    model_b["litellm_params"].pop("input_cost_per_token")  # b drifts (cost dropped)

    plan = reconcile(spec_path, client, dry_run=False)
    diffs = {d.name: d for d in plan.diffs if d.resource_type == "model"}
    assert diffs["b/model"].action is Action.NOOP
    assert "SERVER_DERIVED_PRICING" in diffs["b/model"].message
    # a persisted its cost and drifted nothing: plain NOOP, no acceptance text
    assert diffs["a/model"].action is Action.NOOP
    assert diffs["a/model"].message == ""


def test_version_probe_suppresses_cost_only_drift_upfront(tmp_path):
    """When the read-only version probe announces >= 1.102, a cost-only drift
    is accepted BEFORE patching: no dead PATCH, and --dry-run converges too."""
    client, fake = make_fake_client()
    fake.proxy_version = (1, 104, 2)  # probe-known >= 1.102 generation
    spec_path = _write_spec(
        tmp_path, _spec({"model_name": "openai/gpt-4o", "model_info": {"id": "m5"}})
    )
    reconcile(spec_path, client, dry_run=False)  # create, no costs

    spec_path.write_text(
        json.dumps(
            _spec(
                {
                    "model_name": "openai/gpt-4o",
                    "model_info": {"id": "m5", "input_cost_per_million_tokens": 2.0},
                }
            )
        )
    )
    patch_calls = []
    orig_patch = client.patch_model
    client.patch_model = (  # type: ignore[method-assign]
        lambda *a, **k: (patch_calls.append(a), orig_patch(*a, **k))[1]
    )

    plan = reconcile(spec_path, client, dry_run=False)
    diff = _model_diff(plan)
    assert diff.action is Action.NOOP
    assert "SERVER_DERIVED_PRICING" in diff.message
    assert patch_calls == []  # suppressed BEFORE the patch, not verified after

    # dry-run converges too (this is the fix for the >= 1.102 dry-run wedge)
    plan = reconcile(spec_path, client, dry_run=True)
    diff = _model_diff(plan)
    assert diff.action is Action.NOOP
    assert "SERVER_DERIVED_PRICING" in diff.message
    assert "would be updated" not in str(diff)
    assert patch_calls == []

    # probe-unknown fallback still reaches the apply-then-verify acceptance
    fake.proxy_version = None
    fake.drop_model_costs = True  # fallback cannot know; the fake must drop
    plan = reconcile(spec_path, client, dry_run=False)
    diff = _model_diff(plan)
    assert diff.action is Action.NOOP
    assert "SERVER_DERIVED_PRICING" in diff.message
    assert len(patch_calls) == 1  # one dead PATCH was sent in the fallback


def test_version_probe_mixed_drift_reports_only_appliable_deltas(tmp_path):
    """On a probe-known >= 1.102 proxy the cost keys are dropped from the
    change set, so the plan line describes only what can actually change."""
    client, fake = make_fake_client()
    fake.proxy_version = (1, 104, 2)
    spec_path = _write_spec(
        tmp_path,
        _spec(
            {
                "model_name": "openai/gpt-4o",
                "model_info": {"id": "m6", "mode": "llm"},
            }
        ),
    )
    reconcile(spec_path, client, dry_run=False)

    spec_path.write_text(
        json.dumps(
            _spec(
                {
                    "model_name": "openai/gpt-4o",
                    "model_info": {
                        "id": "m6",
                        "mode": "embedding",
                        "input_cost_per_million_tokens": 2.0,
                    },
                }
            )
        )
    )
    plan = reconcile(spec_path, client, dry_run=True)
    diff = _model_diff(plan)
    assert diff.action is Action.UPDATE
    # costs suppressed from the plan: only the appliable delta is described
    assert set(diff.changes) == {"mode"}
    assert diff.message == ""
    # the non-cost delta applies; a follow-up run then reports no drift at all
    plan = reconcile(spec_path, client, dry_run=False)  # applies mode
    assert set(_model_diff(plan).changes) == {"mode"}
    plan = reconcile(spec_path, client, dry_run=False)  # now converged
    diff = _model_diff(plan)
    assert diff.action is Action.NOOP
    assert diff.changes == {}
