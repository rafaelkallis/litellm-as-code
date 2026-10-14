"""Mock tests for scripts/record_fixtures.py (issue #28).

Everything here runs against a canned HTTP transport — no docker, no live
proxy. The transcript below is a miniature converged variant-E state (same
identifiers/shape conventions as tests/live), which lets the capture → scrub →
posture → replay pipeline be exercised end-to-end where the REAL replay
against committed fixtures is covered by tests/test_response_fixtures.py.

Shaped-by-hand Rules honored here (AGENTS.md §4):
- /budget/list & /organization/list answer bare arrays;
- /user/list & /v2/team/list carry total/total_pages;
- /team/info -> {team_info}; /model/info -> {data};
- credential_values values come back MASKED; key rows never echo `key`.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

_HARNESS_PATH = Path(__file__).resolve().parents[1] / "scripts" / "record_fixtures.py"
spec = importlib.util.spec_from_file_location("record_fixtures", _HARNESS_PATH)
assert spec.loader is not None
record_fixtures = importlib.util.module_from_spec(spec)
sys.modules["record_fixtures"] = record_fixtures  # sibling-script import (tests import by path)
spec.loader.exec_module(record_fixtures)

from litellm_as_code.types import ReconcilerError  # noqa: E402

# server-minted identifiers in the canned transcript (uuid-shaped, so the
# scrubber must syntheticize them — and the replay must use the same labels)
_MODEL_INFO_ID = "cccccccc-2222-3333-4444-555555555555"
_TEAM_ID = "b5a2f1c8-1234-5678-9abc-def012345678"
_KEY_TOKEN = "88998899-8888-8888-8888-888888888888"
_GUARDRAIL_ID = "aabbccdd-1111-2222-3333-444444444444"
_POLICY_ID = "eeefff00-1234-5678-9abc-555555555555"

# masker-echo shapes (LiteLLM _get_masked_values output): 2 kept chars around
# an asterisk run — the exact shape tests/fakes.py encodes by hand today.


def _converged_responses(**overrides: Any) -> dict[str, tuple[int, Any]]:
    """A miniature converged variant-E transcript (all shapes per AGENTS §4)."""
    responses: dict[str, tuple[int, Any]] = {
        "/openapi.json": (200, {"info": {"version": "1.97.0"}}),
        "/budget/list": (
            200,
            [
                {
                    "budget_id": "var-c-budget",
                    "max_budget": 50.0,
                    "soft_budget": 40.0,
                    "tpm_limit": 100000,
                    "rpm_limit": 1000,
                    "budget_duration": "30d",
                    # keys map by deployment id (uuid-shaped dict key)
                    "model_max_budget": {_MODEL_INFO_ID: 0.5},
                    "max_parallel_requests": None,
                }
            ],
        ),
        "/model/info": (
            200,
            {
                "data": [
                    {
                        "model_name": "var-model-c",
                        "litellm_params": {
                            "model": "hosted_vllm/var-base-c",
                            "litellm_credential_name": "var-c-cred",
                        },
                        "model_info": {
                            "id": _MODEL_INFO_ID,
                            "base_model": "var-base-c",
                            "tier": "free",
                            "mode": "completion",
                            # DB-backed rows report authoritative per-million
                            # values plus a recomputed per-token echo
                            "input_cost_per_million_tokens": 2.0,
                            "output_cost_per_million_tokens": 1.0,
                            "input_cost_per_token": 0.000002,
                            "output_cost_per_token": 0.000001,
                        },
                        "input_cost_per_token": 0.000002,
                        "output_cost_per_token": 0.000001,
                        "mode": "completion",
                        "provider": "hosted_vllm",
                    }
                ]
            },
        ),
        "/credentials": (
            200,
            {
                "credentials": [
                    {
                        "credential_name": "var-c-cred",
                        "credential_info": {"custom_llm_provider": "hosted_vllm"},
                        # masked echoes (2 kept chars at each end): the
                        # posture the capture pins
                        "credential_values": {
                            "api_base": "ht********************************v1",
                            "api_key": "sk******************-c",
                        },
                        "model_id": _MODEL_INFO_ID,
                    }
                ]
            },
        ),
        "/organization/list": (200, []),
        "/user/list": (
            200,
            {
                "users": [
                    {
                        "user_id": "var-c-admin",
                        "user_alias": "var-c-admin",
                        "user_email": "var-c-admin@example.com",
                        "user_role": "proxy_admin",
                        "auto_create_key": False,
                        # a plaintext secret nested in a benign container:
                        # dropped + ledgered by the scrub policy
                        "metadata": {"billing_api_key": "sk-billing-plaintext-999"},
                    },
                    {
                        "default_user_id": None,
                        "user_id": "default_user_id",
                        "user_email": "admin@example.com",
                        "user_role": "proxy_admin",
                    },
                ],
                "total": 2,
                "total_pages": 1,
            },
        ),
        "/v2/team/list": (
            200,
            {
                "teams": [
                    {
                        "team_id": _TEAM_ID,
                        "team_alias": "var-team-c",
                        "max_budget": 25.0,
                        "budget_duration": "30d",
                        "models": [],
                        "members_with_roles": [{"user_id": "var-c-admin", "role": "user"}],
                    }
                ],
                "total": 1,
                "total_pages": 1,
            },
        ),
        "/team/info": (
            200,
            {
                "team_info": {
                    "team_id": _TEAM_ID,
                    "team_alias": "var-team-c",
                    "max_budget": 25.0,
                    "budget_duration": "30d",
                    "members_with_roles": [{"user_id": "var-c-admin", "role": "user"}],
                }
            },
        ),
        "/key/list": (
            200,
            {
                "keys": [
                    {
                        "key_alias": "var-c-key",
                        "token": _KEY_TOKEN,
                        "user_id": "var-c-admin",
                        "team_id": _TEAM_ID,
                        "models": [],
                        "allowed_routes": [],
                        "max_budget": None,
                        "spend": 0.0,
                        # NOTE: no `key` field — write-once, never read back
                    }
                ],
                "total_count": 1,
                "total_pages": 1,
            },
        ),
        "/v2/guardrails/list": (
            200,
            {
                "guardrails": [
                    {
                        "guardrail_id": _GUARDRAIL_ID,
                        "guardrail_name": "var-guard-c",
                        "litellm_params": {"guardrail": "presidio", "mode": "pre_call"},
                        "guardrail_info": {"description": "variant C guard"},
                        "default_on": False,
                    }
                ]
            },
        ),
        "/policies/list": (
            200,
            {
                "policies": [
                    {
                        "policy_id": _POLICY_ID,
                        "policy_name": "var-policy-c",
                        "description": "variant E policy",
                        "guardrails_add": ["var-guard-c"],
                        "guardrails_remove": [],
                    }
                ]
            },
        ),
    }
    responses.update(overrides)
    return responses


def _transport_of(responses: dict[str, tuple[int, Any]], state: dict[str, Any] | None = None):
    """Turn a canned path table into a (method, url, params) transport."""

    def transport(method: str, url: str, params: dict[str, Any]) -> tuple[int, Any]:
        if state is not None:
            state["calls"] = state.get("calls", 0) + 1
        path = urlsplit(url).path
        entry = responses.get(path)
        if entry is None:
            return (404, {"error": {"message": f"no canned response for {path}"}})
        mutate = None if state is None else state.get("mutate")
        if mutate is not None:
            status, payload = mutate(path, entry)
            return (status, payload)
        return entry

    return transport


def _capture(responses, state=None) -> dict[str, Any]:
    return record_fixtures.run_capture(
        transport=_transport_of(responses, state),
        litellm_tag="v1.97.0",
    )


# ---------------------------------------------------------------- layout/meta


def test_capture_bundle_writes_every_step_raw_and_normalized():
    bundle = _capture(_converged_responses())
    files = bundle["files"]
    expected = {"_meta.json", "exported_spec.json"}
    for step in record_fixtures.CAPTURE_STEPS:
        expected |= {f"{step}.raw.json", f"{step}.normalized.json"}
    assert set(files) == expected
    meta = bundle["meta"]
    assert meta["harness"] == "record_fixtures"
    assert meta["fixture_version"] == record_fixtures.FIXTURE_VERSION
    assert meta["litellm_tag"] == "v1.97.0"
    assert meta["proxy_openapi_version"] == "1.97.0"
    assert meta["org_gate_recorded"] is False


def test_capture_runs_the_sweep_twice_deterministically():
    # every canned response is served twice (two sweeps); each sweep makes
    # exactly one GET per consumed endpoint (variant-E fits page 1) plus the
    # openapi version probe
    reads_per_sweep = len(record_fixtures.CAPTURE_STEPS) + 1  # steps + team/info is within, openapi below
    state: dict[str, Any] = {}
    _capture(_converged_responses(), state)
    assert state["calls"] == 2 * reads_per_sweep


def test_meta_records_issued_labels_but_never_the_raw_identifiers():
    bundle = _capture(_converged_responses())
    meta_bytes = bundle["files"]["_meta.json"]
    assert _TEAM_ID not in meta_bytes  # server-minted uuid must not enter git
    assert _KEY_TOKEN not in meta_bytes
    assert "var-c-admin@example.com" not in meta_bytes
    issued = bundle["meta"]["syntheticized"]["issued"]
    assert issued == sorted(issued)
    assert len(issued) == bundle["meta"]["syntheticized"]["uuids"] + bundle["meta"][
        "syntheticized"
    ]["emails"]
    assert all(label.startswith(("fixture-uuid-", "scrub-")) for label in issued)


# ---------------------------------------------------------------- scrub policy


def test_scrub_keeps_masker_echos_and_drops_benign_keyed_plaintexts():
    bundle = _capture(_converged_responses())
    cred_raw = json.loads(bundle["files"]["credentials.raw.json"])
    row = cred_raw["credentials"][0]
    # masked echoes survive verbatim (the posture evidence)
    assert row["credential_values"]["api_base"] == "ht********************************v1"
    assert row["credential_values"]["api_key"] == "sk******************-c"
    masked = bundle["meta"]["posture"]["masked_value_paths"]
    assert "credentials.credentials[0].credential_values.api_base" in masked
    assert "credentials.credentials[0].credential_values.api_key" in masked
    # a PLAINTEXT under a sensitive key inside a benign container is dropped
    # and ledgered (metadata.billing_api_key in the canned users row)
    dropped = bundle["meta"]["posture"]["dropped_sensitive_paths"]
    assert "users.users[1].metadata.billing_api_key" in dropped
    users_raw = json.loads(bundle["files"]["users.raw.json"])
    assert users_raw["users"][1]["metadata"]["billing_api_key"] is None


def test_key_tokens_are_dropped_and_key_field_stays_absent():
    bundle = _capture(_converged_responses())
    keys_raw = json.loads(bundle["files"]["keys.raw.json"])
    row = keys_raw["keys"][0]
    assert row["token"] is None  # dropped, never labeled
    assert "key" not in row  # write-once stays write-once
    assert row["key_alias"] == "var-c-key"  # identity survives scrubbing
    dropped = bundle["meta"]["posture"]["dropped_sensitive_paths"]
    assert "keys.keys[0].token" in dropped


def test_uuids_and_emails_become_stable_labels_everywhere():
    bundle = _capture(_converged_responses())
    teams_raw = json.loads(bundle["files"]["teams.raw.json"])
    team_id = teams_raw["teams"][0]["team_id"]
    assert team_id.startswith("fixture-uuid-")
    # the same label holds the team-info map key AND the key row's team_id ref
    info_raw = json.loads(bundle["files"]["team-info.raw.json"])
    assert team_id in info_raw
    keys_raw = json.loads(bundle["files"]["keys.raw.json"])
    assert keys_raw["keys"][0]["team_id"] == team_id
    # budget's model_max_budget map key (deployment id) got the same label as
    # the model row's model_info.id — cross-file reference consistency
    budgets_raw = json.loads(bundle["files"]["budgets.raw.json"])
    model_label = list(budgets_raw[0]["model_max_budget"])[0]
    assert model_label.startswith("fixture-uuid-")
    models_raw = json.loads(bundle["files"]["models.raw.json"])
    assert models_raw["data"][0]["model_info"]["id"] == model_label
    # emails syntheticized in users rows
    users_raw = json.loads(bundle["files"]["users.raw.json"])
    emails = {row.get("user_email") for row in users_raw["users"]}
    issued = set(bundle["meta"]["syntheticized"]["issued"])
    assert emails <= issued
    assert "scrub-1@example.com" in emails


# ---------------------------------------------------------------- posture


def test_capture_fails_when_credential_values_come_back_unmasked():
    responses = _converged_responses(
        **{
            "/credentials": (
                200,
                {
                    "credentials": [
                        {
                            "credential_name": "var-c-cred",
                            "credential_info": {"custom_llm_provider": "hosted_vllm"},
                            "credential_values": {
                                "api_base": "https://variant-c.example.com/v1",
                                "api_key": "sk-var-cred-c",
                            },
                            "model_id": _MODEL_INFO_ID,
                        }
                    ]
                },
            )
        }
    )
    with pytest.raises(record_fixtures.CaptureError, match="stopped masking"):
        _capture(responses)


def test_capture_fails_when_key_rows_echo_their_key():
    responses = _converged_responses(
        **{
            "/key/list": (
                200,
                {
                    "keys": [
                        {
                            "key_alias": "var-c-key",
                            "token": _KEY_TOKEN,
                            "key": "sk-var-c-key-44444444444444444444",
                        }
                    ],
                    "total_count": 1,
                    "total_pages": 1,
                },
            )
        }
    )
    with pytest.raises(record_fixtures.CaptureError, match="echoing key material"):
        _capture(responses)


# ---------------------------------------------------------------- determinism


def test_determinism_gate_reds_on_volatile_payloads():
    counter = {"n": 0}

    def bump_spend(path: str, entry: tuple[int, Any]) -> tuple[int, Any]:
        if path == "/user/list":
            status, payload = entry
            counter["n"] += 1
            rows = [dict(row) for row in payload["users"]]
            rows[0]["spend"] = float(counter["n"])  # a field that moves per read
            return (status, {**payload, "users": rows})
        return entry

    state: dict[str, Any] = {"mutate": bump_spend}
    with pytest.raises(record_fixtures.CaptureError, match="not deterministic"):
        _capture(_converged_responses(), state)


def test_determinism_gate_passes_on_stable_payloads():
    _capture(_converged_responses())  # must not raise


# ---------------------------------------------------------------- org gate


_GATE_BODY = {
    "error": {
        "message": "Organizations are only available for LiteLLM Enterprise users. "
        "Please obtain a license key and set it in proxy_config."
    }
}


def test_org_gate_403_is_recorded_not_fatal():
    responses = _converged_responses(**{"/organization/list": (403, _GATE_BODY)})
    bundle = _capture(responses)
    org_raw = json.loads(bundle["files"]["organizations.raw.json"])
    assert org_raw["fixture_gate_error"] is True
    assert org_raw["status"] == 403
    assert "Enterprise" in org_raw["detail"]
    assert bundle["meta"]["org_gate_recorded"] is True
    # exported spec carries no organizations (capability tolerated the gate)
    exported = json.loads(bundle["files"]["exported_spec.json"])
    assert "organizations" not in exported or exported["organizations"] in ([], None)
    normalized = json.loads(bundle["files"]["organizations.normalized.json"])
    assert normalized == {"fixture_gate_error": True}


def test_gate_detail_extraction_strips_the_stack_url():
    exc = ReconcilerError(
        "GET /organization/list failed: 403 Client Error: Forbidden for url: "
        "http://localhost:4001/organization/list {'error': 'Organizations are "
        "only available for LiteLLM Enterprise users.'}"
    )
    detail = record_fixtures._gate_detail(exc)
    assert detail == "{'error': 'Organizations are only available for LiteLLM Enterprise users.'}"
    assert "localhost" not in detail


# ---------------------------------------------------------------- replay


def test_fixture_client_serves_recorded_envelopes_and_parses_them():
    bundle = _capture(_converged_responses())
    steps = bundle["steps"]
    client = record_fixtures.FixtureClient.from_payloads(steps, "1.97.0")
    assert client.proxy_version() == (1, 97, 0)
    users = client.list_users()
    assert [row["user_id"] for row in users] == ["default_user_id", "var-c-admin"]
    teams = client.list_teams()
    assert teams[0]["team_alias"] == "var-team-c"
    # get_team_info unwraps the recorded envelope through the REAL unwrap
    team_id = teams[0]["team_id"]
    assert client.get_team_info(team_id)["members_with_roles"] == [
        {"user_id": "var-c-admin", "role": "user"}
    ]
    assert client.list_keys()[0]["key_alias"] == "var-c-key"


def test_fixture_client_pagination_serves_recorded_page1_only():
    bundle = _capture(_converged_responses())
    client = record_fixtures.FixtureClient.from_payloads(
        bundle["steps"], bundle["meta"]["proxy_openapi_version"]
    )
    page2 = client._request("GET", "/user/list", params={"page": 2})
    assert page2["users"] == []  # empty page -> loop terminates


def test_fixture_client_counts_and_refuses_mutations():
    bundle = _capture(_converged_responses())
    client = record_fixtures.FixtureClient.from_payloads(bundle["steps"], "1.97.0")
    assert client.writes == 0
    with pytest.raises(ReconcilerError, match="only GET is served"):
        client._request("POST", "/model/new", json={})
    assert client.writes == 1


def test_fixture_client_replays_the_org_gate_error():
    responses = _converged_responses(**{"/organization/list": (403, _GATE_BODY)})
    bundle = _capture(responses)
    client = record_fixtures.FixtureClient.from_payloads(bundle["steps"], "1.97.0")
    with pytest.raises(ReconcilerError) as excinfo:
        client._request("GET", "/organization/list")
    message = str(excinfo.value)
    # the canonical gate markers survive the replay byte-for-byte from the body
    assert "403" in message
    assert "enterprise" in message.lower()


def test_fixture_client_reports_unknown_team_and_unknown_path():
    bundle = _capture(_converged_responses())
    client = record_fixtures.FixtureClient.from_payloads(bundle["steps"], "1.97.0")
    with pytest.raises(ReconcilerError, match="no replay payload"):
        client._request("GET", "/team/info", params={"team_id": "missing"})
    with pytest.raises(ReconcilerError, match="no recorded payload"):
        client._request("GET", "/somewhere/new")


# ---------------------------------------------------------------- exported


def test_exported_spec_skips_the_default_user_and_uses_labels():
    bundle = _capture(_converged_responses())
    exported = json.loads(bundle["files"]["exported_spec.json"])
    assert [u["user_id"] for u in exported["users"]] == ["var-c-admin"]
    assert "default_user_id" not in [u["user_id"] for u in exported["users"]]
    # currency conversion: live per-token -> spec per-million (x 1e6)
    model = exported["models"][0]
    assert model["model_info"]["input_cost_per_million_tokens"] == pytest.approx(2.0)
    assert model["model_info"]["output_cost_per_million_tokens"] == pytest.approx(1.0)
    assert model["model_name"] == "var-model-c"
    # secrets: export writes credential_values as {} (masked, write-once)
    cred = exported["credentials"][0]
    assert cred["credential_values"] == {}
    # team/export references consistent labels
    team = exported["teams"][0]
    assert team["team_id"].startswith("fixture-uuid-")
    assert team["members_with_roles"] == [{"user_id": "var-c-admin", "role": "user"}]


def test_run_capture_is_replay_safe_for_the_mock_suite():
    """The exact objects the committed replay suite will consume."""
    bundle = _capture(_converged_responses())
    client = record_fixtures.FixtureClient.from_payloads(
        bundle["steps"], bundle["meta"]["proxy_openapi_version"]
    )
    # normalized derivation is reproducible in-memory (same runs as files)
    assert client.list_budgets() == json.loads(bundle["files"]["budgets.normalized.json"])
    assert client.list_models() == json.loads(bundle["files"]["models.normalized.json"])
    assert client.list_organizations() == json.loads(
        bundle["files"]["organizations.normalized.json"]
    )
