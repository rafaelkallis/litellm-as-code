"""Spec validation tests (Pydantic per-resource schemas).

Covers the declarative validation added in `litellm_as_code.validation` and
wired into `litellm_as_code.spec.load_spec`:

- required identity fields per resource,
- type checks for the manageable fields,
- enum checks (roles),
- unknown top-level section -> hard SpecError,
- unknown per-resource key -> warning only (spec still loads),
- nested opaque payloads are pass-through (no spurious errors),
- all errors collected & reported in one pass.

Mirrors the "mock-only unit tests" convention of the rest of the repo.
"""

from __future__ import annotations

import copy
import json

import pytest

from litellm_as_code.spec import SpecError, load_spec

# A fully-valid spec spanning every reconcilable section (mirrors
# examples/spec.yml and exercises every typed/modeled field).
VALID_SPEC = {
    "config": {"general_settings": {"store_model_in_db": True}},
    "budgets": [
        {"budget_id": "platform-budget", "max_budget": 100.0, "budget_duration": "30d"},
    ],
    "organizations": [
        {
            "organization_id": "org-acme",
            "organization_alias": "acme",
            "members_with_roles": [
                {"user_id": "username-admin", "role": "org_admin"},
                {"user_id": "username-service", "role": "internal_user"},
            ],
        }
    ],
    "users": [
        {
            "user_id": "username-admin",
            "user_alias": "admin",
            "user_email": "admin@example.com",
            "user_role": "proxy_admin",
            "auto_create_key": "false",
        }
    ],
    "teams": [
        {
            "team_id": "team-prod",
            "team_alias": "production",
            "members_with_roles": [
                {"user_id": "username-admin", "role": "admin"},
                {"user_id": "username-service", "role": "user"},
            ],
        }
    ],
    "virtual_keys": [
        {
            "key_alias": "admin-cli",
            "key": "sk-example-key-value-0000000000000000",
            "user_id": "username-admin",
        }
    ],
    "credentials": [
        {
            "credential_name": "my-vllm",
            "credential_info": {"custom_llm_provider": "hosted_vllm"},
            "credential_values": {
                "api_base": "http://my-vllm:8000/v1",
                "api_key": "sk-provider-key",
            },
        }
    ],
    "models": [
        {
            "model_name": "myorg/chat",
            "model_info": {
                "id": "11111111-2222-3333-4444-555555555555",
                "mode": "chat",
                "base_model": "some-chat-model",
                "input_cost_per_million_tokens": 3.0,
                "output_cost_per_million_tokens": 15.0,
            },
            "litellm_params": {
                "model": "hosted_vllm/some-chat-model",
                "litellm_credential_name": "my-vllm",
            },
        }
    ],
    "guardrails": [
        {
            "guardrail_name": "pii-guard",
            "litellm_params": {"guardrail": "presidio", "mode": "pre_call"},
            "guardrail_info": {"description": "PII masking"},
        }
    ],
    "policies": [
        {
            "policy_name": "global-baseline",
            "description": "Base guardrails for all requests",
            "guardrails_add": ["pii-guard"],
            "guardrails_remove": [],
        }
    ],
}


def _tmp_spec(tmp_path, data) -> str:
    path = tmp_path / "spec.yml"
    path.write_text(json.dumps(data))
    return str(path)


def test_valid_full_spec_loads(tmp_path):
    load_spec(_tmp_spec(tmp_path, VALID_SPEC))  # must not raise


def test_valid_full_spec_is_passed_through(tmp_path):
    """Validated entries are returned verbatim (dict in == dict out)."""
    spec = load_spec(_tmp_spec(tmp_path, VALID_SPEC))
    assert spec["users"][0]["user_id"] == "username-admin"
    assert spec["credentials"][0]["credential_values"]["api_key"] == "sk-provider-key"
    assert spec["models"][0]["litellm_params"]["model"] == "hosted_vllm/some-chat-model"


def test_omitted_or_null_section_passes_validation(tmp_path):
    """An explicitly-null section (YAML `users:` with no value) reaches
    validate_spec as None and passes cleanly: the `data.get(section, []) or
    []` normalization already turns None into [] — which is what made the
    `if entries is None` branch dead code (issue #12 Part C, pinned here so
    any future normalization change must account for it)."""
    load_spec(_tmp_spec(tmp_path, {"users": None}))  # must not raise


# -- required identity fields ------------------------------------------------


@pytest.mark.parametrize(
    ("section", "entry"),
    [
        ("users", {"user_alias": "admin"}),
        ("virtual_keys", {"user_id": "u1"}),
        ("credentials", {"credential_info": {}}),
        ("models", {"model_info": {}}),
        ("guardrails", {"guardrail_info": {}}),
        ("policies", {"policy_name": None}),  # set but null -> missing
    ],
)
def test_missing_required_identity_raises(tmp_path, section, entry):
    data = copy.deepcopy(VALID_SPEC)
    data[section] = [entry]
    with pytest.raises(SpecError, match=section):
        load_spec(_tmp_spec(tmp_path, data))


def test_team_requires_id_or_alias(tmp_path):
    data = copy.deepcopy(VALID_SPEC)
    data["teams"] = [{"organization_id": "o1"}]
    with pytest.raises(SpecError, match="team_alias|team_id"):
        load_spec(_tmp_spec(tmp_path, data))


def test_organization_requires_id_or_alias(tmp_path):
    data = copy.deepcopy(VALID_SPEC)
    data["organizations"] = [{"models": ["m1"]}]
    with pytest.raises(SpecError, match="organization_id|organization_alias"):
        load_spec(_tmp_spec(tmp_path, data))


def test_budget_requires_budget_id(tmp_path):
    data = copy.deepcopy(VALID_SPEC)
    data["budgets"] = [{"max_budget": 25.0}]
    with pytest.raises(SpecError, match="budget_id"):
        load_spec(_tmp_spec(tmp_path, data))


def test_budget_with_null_budget_id_requires_budget_id(tmp_path):
    """Set-but-null counts as missing (matches the policies entry above)."""
    data = copy.deepcopy(VALID_SPEC)
    data["budgets"] = [{"budget_id": None, "max_budget": 25.0}]
    with pytest.raises(SpecError, match="budget_id"):
        load_spec(_tmp_spec(tmp_path, data))


# -- cross-entry uniqueness ---------------------------------------------------


@pytest.mark.parametrize(
    ("section", "field", "entry"),
    [
        ("budgets", "budget_id", {"budget_id": "dup", "max_budget": 1.0}),
        ("users", "user_id", {"user_id": "dup"}),
        ("credentials", "credential_name", {"credential_name": "dup"}),
        ("models", "model_name", {"model_name": "dup"}),
        ("guardrails", "guardrail_name", {"guardrail_name": "dup"}),
        ("policies", "policy_name", {"policy_name": "dup"}),
    ],
)
def test_duplicate_identity_across_entries(tmp_path, section, field, entry):
    """Two entries with the same identity would reconcile last-wins on the
    same live resource; caught at spec-load, naming both indices."""
    data = copy.deepcopy(VALID_SPEC)
    data[section] = [entry, entry]
    with pytest.raises(
        SpecError,
        match=rf"duplicate {field} .*first declared at spec\.{section}\[0\]",
    ):
        load_spec(_tmp_spec(tmp_path, data))


def test_duplicate_key_alias_names_both_indices(tmp_path):
    data = copy.deepcopy(VALID_SPEC)
    data["virtual_keys"] = [
        {"key_alias": "admin-cli", "user_id": "username-admin"},
        {"key_alias": "admin-cli", "user_id": "username-admin"},
    ]
    with pytest.raises(
        SpecError,
        match=r"duplicate key_alias .*first declared at spec\.virtual_keys\[0\]",
    ):
        load_spec(_tmp_spec(tmp_path, data))


def test_two_alias_only_teams_with_same_alias(tmp_path):
    """The wrong-live-team clobber case: entry A creates team X; entry B
    (same alias) would _find_remote-match X and update it instead of
    creating a second team."""
    data = copy.deepcopy(VALID_SPEC)
    data["teams"] = [
        {"team_alias": "production", "max_budget": 1.0},
        {"team_alias": "production", "max_budget": 2.0},
    ]
    with pytest.raises(SpecError, match="duplicate team_alias 'production'"):
        load_spec(_tmp_spec(tmp_path, data))


def test_id_carrying_team_and_alias_only_team_sharing_alias(tmp_path):
    data = copy.deepcopy(VALID_SPEC)
    data["teams"] = [
        {"team_id": "team-prod", "team_alias": "production"},
        {"team_alias": "production", "max_budget": 5.0},
    ]
    with pytest.raises(SpecError, match="duplicate team_alias 'production'"):
        load_spec(_tmp_spec(tmp_path, data))


def test_two_alias_only_orgs_with_same_alias(tmp_path):
    data = copy.deepcopy(VALID_SPEC)
    data["organizations"] = [
        {"organization_alias": "acme", "models": ["m1"]},
        {"organization_alias": "acme", "models": ["m2"]},
    ]
    with pytest.raises(SpecError, match="duplicate organization_alias 'acme'"):
        load_spec(_tmp_spec(tmp_path, data))


def test_id_carrying_org_and_alias_only_org_sharing_alias(tmp_path):
    data = copy.deepcopy(VALID_SPEC)
    data["organizations"] = [
        {"organization_id": "org-acme", "organization_alias": "acme"},
        {"organization_alias": "acme", "models": ["m2"]},
    ]
    with pytest.raises(SpecError, match="duplicate organization_alias 'acme'"):
        load_spec(_tmp_spec(tmp_path, data))


def test_orgs_with_same_id_but_different_alias(tmp_path):
    data = copy.deepcopy(VALID_SPEC)
    data["organizations"] = [
        {"organization_id": "org-x", "organization_alias": "alpha"},
        {"organization_id": "org-x", "organization_alias": "beta"},
    ]
    with pytest.raises(SpecError, match="duplicate organization_id 'org-x'"):
        load_spec(_tmp_spec(tmp_path, data))


def test_teams_with_same_id_but_different_alias(tmp_path):
    data = copy.deepcopy(VALID_SPEC)
    data["teams"] = [
        {"team_id": "team-x", "team_alias": "alpha"},
        {"team_id": "team-x", "team_alias": "beta"},
    ]
    with pytest.raises(SpecError, match="duplicate team_id 'team-x'"):
        load_spec(_tmp_spec(tmp_path, data))


def test_teams_with_distinct_ids_and_aliases_load(tmp_path):
    data = copy.deepcopy(VALID_SPEC)
    data["teams"] = [
        {"team_alias": "alpha"},
        {"team_id": "team-beta", "team_alias": "beta"},
    ]
    load_spec(_tmp_spec(tmp_path, data))  # must not raise


def test_duplicate_identity_is_skipped_for_null_values(tmp_path):
    """Null/absent identity never compares against anything (the per-entry
    identity validators catch those cases separately)."""
    data = copy.deepcopy(VALID_SPEC)
    data["policies"] = [{"policy_name": None}, {"guardrails_add": []}]
    with pytest.raises(SpecError, match="policy_name"):
        load_spec(_tmp_spec(tmp_path, data))


@pytest.mark.parametrize(
    ("section", "field"),
    [
        ("users", "user_id"),
        ("virtual_keys", "key_alias"),
        ("credentials", "credential_name"),
        ("models", "model_name"),
        ("guardrails", "guardrail_name"),
        ("policies", "policy_name"),
        ("budgets", "budget_id"),
        ("teams", "team_id"),
        ("teams", "team_alias"),
        ("organizations", "organization_id"),
        ("organizations", "organization_alias"),
    ],
)
def test_empty_string_identity_is_rejected(tmp_path, section, field):
    """An empty-string identity is not a valid identity: it would reconcile
    against a bogus empty live identifier and must not be able to slip past
    (or through) the cross-entry uniqueness check."""
    data = copy.deepcopy(VALID_SPEC)
    entry = dict.fromkeys((field,))
    entry[field] = ""
    data[section] = [entry]
    with pytest.raises(SpecError, match=field):
        load_spec(_tmp_spec(tmp_path, data))


def test_empty_string_member_user_id_is_rejected(tmp_path):
    data = copy.deepcopy(VALID_SPEC)
    data["teams"] = [
        {
            "team_id": "team-prod",
            "members_with_roles": [{"user_id": "", "role": "admin"}],
        }
    ]
    with pytest.raises(SpecError, match="user_id"):
        load_spec(_tmp_spec(tmp_path, data))


def test_duplicate_team_member_user_id_raises(tmp_path):
    """The reconciler builds want_by_id = {user_id: role} — a repeated
    user_id would silently keep the last role."""
    data = copy.deepcopy(VALID_SPEC)
    data["teams"] = [
        {
            "team_id": "team-prod",
            "members_with_roles": [
                {"user_id": "u1", "role": "admin"},
                {"user_id": "u1", "role": "user"},
            ],
        }
    ]
    with pytest.raises(SpecError, match="duplicate user_id 'u1'"):
        load_spec(_tmp_spec(tmp_path, data))


def test_duplicate_org_member_user_id_raises(tmp_path):
    data = copy.deepcopy(VALID_SPEC)
    data["organizations"] = [
        {
            "organization_id": "org-acme",
            "members_with_roles": [
                {"user_id": "u1", "role": "org_admin"},
                {"user_id": "u1", "role": "internal_user"},
            ],
        }
    ]
    with pytest.raises(SpecError, match="duplicate user_id 'u1'"):
        load_spec(_tmp_spec(tmp_path, data))


# -- type & enum checks ------------------------------------------------------


def test_wrong_type_is_an_error(tmp_path):
    data = copy.deepcopy(VALID_SPEC)
    data["budgets"] = [{"budget_id": "b1", "max_budget": "not-a-number"}]
    with pytest.raises(SpecError, match="max_budget"):
        load_spec(_tmp_spec(tmp_path, data))


def test_bad_user_role_is_an_error(tmp_path):
    data = copy.deepcopy(VALID_SPEC)
    data["users"][0]["user_role"] = "superadmin"
    with pytest.raises(SpecError) as exc:
        load_spec(_tmp_spec(tmp_path, data))
    assert "user_role" in str(exc.value)


def test_bad_team_member_role_is_an_error(tmp_path):
    data = copy.deepcopy(VALID_SPEC)
    data["teams"][0]["members_with_roles"][0]["role"] = "owner"
    with pytest.raises(SpecError, match="role"):
        load_spec(_tmp_spec(tmp_path, data))


def test_section_must_be_a_list(tmp_path):
    data = copy.deepcopy(VALID_SPEC)
    data["users"] = {"user_id": "u1"}  # not a list
    with pytest.raises(SpecError, match="users"):
        load_spec(_tmp_spec(tmp_path, data))


def test_entry_must_be_a_mapping(tmp_path):
    data = copy.deepcopy(VALID_SPEC)
    data["users"] = ["u1"]
    with pytest.raises(SpecError):
        load_spec(_tmp_spec(tmp_path, data))


# -- all errors collected in one pass ----------------------------------------


def test_multiple_errors_reported_at_once(tmp_path):
    data = copy.deepcopy(VALID_SPEC)
    # 3 independent problems across different sections:
    data["users"] = [{"user_alias": "no-id"}]  # missing user_id
    data["virtual_keys"] = [{"user_id": "u1"}]  # missing key_alias
    data["teams"] = [{"members_with_roles": [{"role": "bad"}]}]  # missing id + bad role
    with pytest.raises(SpecError) as exc:
        load_spec(_tmp_spec(tmp_path, data))
    msg = str(exc.value)
    # Every problem surfaced in one SpecError.
    assert "users[0].user_id" in msg
    assert "virtual_keys[0].key_alias" in msg
    assert "teams[0]" in msg
    assert "role" in msg


# -- unknown sections & keys -------------------------------------------------


def test_unknown_top_level_section_is_hard_error(tmp_path):
    data = copy.deepcopy(VALID_SPEC)
    data["spaceships"] = []
    with pytest.raises(SpecError, match="unknown top-level sections"):
        load_spec(_tmp_spec(tmp_path, data))


def test_unknown_per_resource_key_is_warning_only(tmp_path, capsys):
    """Unknown per-resource keys warn but the spec still loads."""
    data = copy.deepcopy(VALID_SPEC)
    data["users"][0]["fancy_new_litellm_field"] = "x"
    spec = load_spec(_tmp_spec(tmp_path, data))

    # Loaded fine, extra key preserved verbatim for the reconciler.
    assert spec["users"][0]["fancy_new_litellm_field"] == "x"
    err = capsys.readouterr().err
    assert "[warn]" in err
    assert "fancy_new_litellm_field" in err


def test_unknown_keys_warn_without_blocking_when_also_valid(tmp_path, capsys):
    data = copy.deepcopy(VALID_SPEC)
    data["models"][0]["litellm_params"]["extra_provider_param"] = {"nested": True}
    data["credentials"][0]["credential_values"]["new_provider_value"] = "secret"
    load_spec(_tmp_spec(tmp_path, data))  # must not raise
    # Opaque nested maps never produce spurious warnings.
    assert "extra_provider_param" not in capsys.readouterr().err


# -- top-level structure (existing behavior preserved) -----------------------


def test_empty_spec_raises(tmp_path):
    with pytest.raises(SpecError, match="empty"):
        load_spec(_tmp_spec(tmp_path, None))


def test_non_mapping_spec_raises(tmp_path):
    with pytest.raises(SpecError, match="mapping"):
        load_spec(_tmp_spec(tmp_path, ["not", "a", "mapping"]))
