# AGENTS.md — litellm-as-code developer & agent guide

This file is the source of truth for anyone (human or coding agent) working on
this repository. Read it before editing code.

## 1. What this project is

A declarative, Terraform-like reconciler for the **runtime state** of a
LiteLLM proxy: you declare users/teams/keys/credentials/models in a YAML spec,
and `litellm-as-code` diffs that spec against the live proxy and applies only
the deltas. It is NOT a configuration generator and it does NOT manage the
proxy's startup `config.yaml`.

## 2. Hard scope boundaries (do not "fix" these)

- **Startup `config.yaml` (general_settings / litellm_settings /
  router_settings) is out of scope.** Those are applied at proxy boot and are
  not reconciliable over the admin REST API. The spec may contain a `config:`
  section, but the reconciler ignores it. Do not add code that reads it.
- **Secret material is never read back from the API.** `credential_values`
  are returned masked (never plaintext) and a key's raw `key` value comes back
  absent. That is deliberate LiteLLM design. Secrets are therefore **write-once**:
  diffing them against the live API is neither possible nor attempted. Do not
  attempt to "fix" drift by re-reading secrets from the API.
- **Never send `key` on `/key/update`** — updates must only touch comparable
  fields; sending `key` would re-assert/rotate it. The reconciler only sends
  `key` on create (`POST /key/generate`). Preserve this.

## 3. The convergence model

```
spec.yml  --diff-->  live API  --apply-->  converge
```

Ordering is fixed: `budgets -> models -> credentials -> organizations (+
members) -> users -> teams (+ members) -> keys -> guardrails -> policies`.
Acyclic & single-target; do not add a graph solver. Models come before
credentials because a credential's `model_id` must reference an existing
model (POST /credentials 404s otherwise).

Identity is **fully API-derived** — there is no local applied-state file:
- key existence: `key_alias` (uniqueness enforced by the proxy) from `/key/list`;
- credential existence: `credential_name` (unique column) from `/credentials`;
- user/team/model existence: `user_id` / `team_id` / `model_name` from `/user/list`,
  `/v2/team/list`, `/model/info`.

The single live API decides drift for all *comparable* (non-secret) fields.
Secret fields are write-once: sent on create (keys) or re-asserted only when a
comparable change already triggered an update (credentials, via idempotent PATCH).
No state file, so there is nothing to lose or drift out of band.

Spec validation: `load_spec` runs declarative per-resource validation
(`litellm_as_code/validation.py`, Pydantic) *before* any API call. It enforces
required identity fields (matching what reconcilers index — a missing identity
is caught here, not as a bare `KeyError` mid-reconcile), types, and role enums,
and it **collects all errors in one pass** (no fail-fast). Unknown **top-level
sections are a hard `SpecError`**; unknown **per-resource keys are non-fatal
warnings** (entries pass through verbatim — keep `extra="allow"` on the models
or reconcilers will receive stripped dicts). Nested opaque payloads
(`credential_values`, `litellm_params`, `model_info`) are deliberately
un-schematized. Rejecting/relaxing validation requires touching `spec.py` +
`validation.py` + `tests/test_spec_validation.py` together.

## 4. LiteLLM API quirks (must-know, learned the hard way)

Nested read envelopes — the reconciler must unwrap these:
- `/key/info` -> `{ "info": {...} }` (raw key lives under `info.token`)
- `/user/info` -> `{ "user_info": {...} }`
- `/team/info` -> `{ "team_info": {...} }`
- `/model/info` (list) -> `{ "data": [...] }`
- `/tag/info` -> map keyed by tag name

Credential masking: `GET /credentials` and `/credentials/by_name/{name}` return
`credential_values` with each value masked (`_get_masked_values`), never
plaintext. Never treat those masked values as desired state, and never diff
`credential_values` against live.

Server-injected defaults: LiteLLM may inject `default_user_id`,
`budget_duration`, `mode`, and metadata interns (`tpm_limit_type`,
`rpm_limit_type`) on read. **Never adopt these into desired state.** Diff only
the manageable fields (see per-resource `COMPARABLE` lists); runtime metrics
(`spend`, `updated_at`, `status`, `budget_reset_at`) are read-only — do not
add them to diffs.

Eventual consistency: after `POST /user/new` / `/team/new` / `/model/new`, a
read-back can 404 briefly. `api.LiteLLMClient` retries with exponential
backoff. Keep new resources on the retry path.

## 5. Per-resource identity & drift (keep in sync with resources/*.py)

| resource | identity | comparable fields |
|---|---|---|
| user | `user_id` (stable UUID) | user_alias, user_email, user_role, auto_create_key |
| team | `team_id` (or `team_alias` fallback) | team_alias, organization_id, max_budget, budget_duration, models |
| team member | (team_id, user_id) | role |
| key | `key_alias` | user_id, team_id, models, max_budget, budget_duration, allowed_routes |
| credential | `credential_name` | credential_info, model_id (values are write-once) |
| model | `model_name` | mode, base_model, tier, custom_llm_provider, litellm_credential_name, costs |

Model cost conversion: spec uses `input_cost_per_million_tokens` /
`output_cost_per_million_tokens` (ecosystem convention); the API wants
per-token. Convert ÷1e6 when sending, and back when comparing.

## 6. CLI contract (stable)

```
litellm-as-code <spec> [--base-url URL] [--api-key KEY] [--dry-run] [--prune]
```

Env aliases: `LITELLM_SPEC`, `LITELLM_BASE_URL`/`BASE_URL`, `LITELLM_API_KEY`/`API_KEY`.

Exit codes: `0` = clean/no diff (applied or no-op); `1` = error;
`2` = plan/apply showed changes **in `--dry-run`** (CI-friendly, like
`terraform plan`'s non-zero diff detection).

`--prune` is reserved but effectively no-op today (additive reconcile only).

## 7. Code conventions

- Python >=3.10, typed with `from __future__ import annotations`.
- `my_conf`: students of Google Python Style (line length split), classes
  CamelCase, methods snake_case. Keep diffs tight — diff only COMPARABLE fields.
- Runtime deps: `requests`, `PyYAML`, and `pydantic` (used for declarative
  spec validation in `litellm_as_code/validation.py`). Dev deps: `pytest`,
  `pytest-mock`.
- Tests are **mock-only** (no live LiteLLM). Add tests under `tests/` that
  mirror `litellm_as_code/` 1:1.
- `LiteLLMClient` is the only place that talks HTTP; resources must go through
  it (never raw `requests`).

## 8. Ecosystem references (source of truth for API surface)

- Main proxy: `github.com/BerriAI/litellm` (MIT outside `enterprise/`).
- The `terraform-provider-litellm` project (`ncecere/terraform-provider-litellm`)
  documents the *same* admin REST endpoints & their quirks; when in doubt about
  an endpoint's exact payload/response shape or a litellm version's behavior,
  check its `litellm/` + `docs/` first.
- Skills pattern: `github.com/BerriAI/litellm-skills` (flat `<skill>/SKILL.md`
  layout, YAML frontmatter) if this repo ever ships agent skills.

## 9. Version pinning

litellm's admin API is only loosely coupled to semver. We pin the proxy image
used for integration testing; when adding/adjusting endpoints, note the
litellm version it was verified against in the commit message and README.
