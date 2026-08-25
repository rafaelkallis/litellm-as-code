# Drift & apply model

`litellm-as-code` is a declarative reconciler, like Terraform for a LiteLLM
proxy's runtime state:

```
spec.yml  --diff-->  live API  --apply-->  converge
```

## Identity & drift (all API-derived, no state file)

There is **no local applied-state file** — the live proxy is the single source
of truth. Existence is determined by API lookups against stable identity fields:

| resource | identity | comparable fields (diffed) |
|---|---|---|
| user | `user_id` | user_alias, user_email, user_role, auto_create_key |
| team | `team_id` (or `team_alias` fallback) | team_alias, organization_id, max_budget, budget_duration, models |
| team member | `(team_id, user_id)` | role |
| key | `key_alias` (proxy-enforced unique) | user_id, team_id, models, max_budget, budget_duration, allowed_routes |
| credential | `credential_name` | credential_info, model_id (values are write-once) |
| model | `model_name` | mode, base_model, tier, custom_llm_provider, litellm_credential_name, costs |

Key/credential existence is read via `key_alias` / `credential_name`; user/team/
model via `user_id` / `team_id` / `model_name`.

## What is NEVER diffed

- **Secrets.** `credential_values` come back masked, and a key's raw `key`
  comes back absent. Secrets are **write-once**: sent on create (keys) or
  re-asserted only via idempotent PATCH when a comparable change already
  triggered an update (credentials). Never re-read secrets to "fix drift";
  that is deliberately impossible.
- **Runtime metrics** (`spend`, `updated_at`, `status`, `budget_reset_at`) and
  **server-injected defaults** (`default_user_id`, `budget_duration`, `mode`,
  `tpm_limit_type`, `rpm_limit_type`). Never adopt these into desired state.

## The apply flow

1. `--dry-run` prints the exact diff, applies nothing.
2. New resources are created; existing ones are updated only on comparable
   field drift.
3. Idempotent — a second run with no changes is a no-op.
4. `--prune` is **reserved but effectively a no-op today** (additive reconcile
   only) — it does not delete resources absent from the spec.

## Exit codes (CI-friendly)

| code | meaning |
|---|---|
| `0` | clean / no diff (applied or no-op) |
| `1` | error |
| `2` | changes were shown by `--dry-run` |

Exit `2` on `--dry-run` is the "a diff exists" signal (like `terraform plan`
returning non-zero) — it is **not** an error, just "there is work to do."

## Reconcile ordering (fixed)

`budgets` → `models` → `credentials` → `organizations` (+ members) → `users` →
`teams` (+ members) → `virtual_keys` → `guardrails` → `policies`.

Models before credentials matters: a credential's `model_id` must reference an
existing model (`POST /credentials` 404s otherwise). Acyclic, single-target.

## Proxy quirks to respect

- **Nested read envelopes** the API returns, which the reconciler unwraps:
  `/key/info` → `info`, `/user/info` → `user_info`, `/team/info` →
  `team_info`, `/model/info` → `data`, `/tag/info` → map by tag.
- **Credential masking:** `GET /credentials` returns masked values only. Never
  treat those as desired state.
- **Eventual consistency:** right after `POST /user/new`, `/team/new`,
  `/model/new` a read-back can briefly 404. `LiteLLMClient` retries with
  exponential backoff. If a fresh resource is missing, allow for this.

## Worked example

**Goal:** change the `max_budget` of team `team-prod` from `100.0` to `150.0`.

1. Edit `spec.yml`:

   ```yaml
   teams:
     - team_id: "team-prod"
       max_budget: 150.0
   ```

2. Plan:

   ```bash
   litellm-as-code spec.yml --dry-run
   # diff: team team-prod max_budget 100.0 -> 150.0
   # exit code 2  (a diff exists — expected)
   ```

3. Apply:

   ```bash
   litellm-as-code spec.yml
   # exit code 0 after the update
   ```
