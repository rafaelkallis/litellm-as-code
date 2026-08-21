# Live-proxy permutation test specs

These specs are **testing artifacts**, not documentation — they exist to
permutation-test `litellm-as-code` against a **real LiteLLM proxy** (see
`examples/docker-compose/` for the documented example). They deliberately
exercise alternate value shapes, identity styles, and update paths that the
documentation example does not.

They are NOT part of the mock-only unit suite (`pytest` ignores this folder —
the tests in `tests/*.py` use the in-memory fake). Run them manually against a
live stack:

```bash
cd examples/docker-compose
docker compose up -d postgres litellm        # live proxy on :4000

# plan first (exit 2 on diff like terraform plan), then apply twice:
/path/to/.venv/bin/litellm-as-code \
  ../../tests/live/spec-variant-a.yml \
  --base-url http://localhost:4000 --api-key "$LITELLM_MASTER_KEY" --dry-run
/path/to/.venv/bin/litellm-as-code \
  ../../tests/live/spec-variant-a.yml \
  --base-url http://localhost:4000 --api-key "$LITELLM_MASTER_KEY"
```

A converged apply prints `0 to create, 0 to update, N unchanged` and a
subsequent `--dry-run` exits `0`.

## What each variant covers

| spec | purpose |
|---|---|
| `spec-variant-a.yml` | Fresh resources with alternate identity styles: no `user_email`/`auto_create_key` on a user, alias-only team, server-minted key (no caller-supplied `key`), explicit empty lists (`models: []`, `allowed_routes: []`), budget without `budget_duration`, **non-zero** per-million model costs, org with members, guardrail with `guardrail_info`, policy with only `guardrails_remove`. |
| `spec-variant-b.yml` | Fixed `team_id` + explicit budgets, team-bound key with non-empty `models`/`allowed_routes`, user with `auto_create_key: true`, tpm/rpm-limited budget, tier'd chat model, org **without** members, policy with `inherit`, credential **binding a model** via `model_id`. |
| `spec-variant-c.yml` | Every optional field expressed explicitly (full field matrix), per-token cost convention in `model_info`, alias-only org, policy with both `guardrails_add` + `guardrails_remove`. |
| `spec-variant-d.yml` | **Mutation round**: renames (team/user/org aliases), `user_role` change, team member role change, member addition, key `allowed_routes` change, budget value changes, model cost flip + `base_model` change, credential provider change, policy drift (triggers recreate). Intentionally conflicts with `spec-variant-b` on shared resources — do not apply both in one run. |

## Server capability caveats (found live, not reconciler bugs)

- Team member role `admin` is **enterprise-only** on self-hosted LiteLLM
  (`Assigning team admins is a premium feature`). For OSS/self-hosted, use
  role `user` in `members_with_roles`.
- `model_info.tier` is an API enum — only `free` | `paid` (validation rejects
  other values at spec load).
- These specs assert a *desired* state; if two specs manage the same
  resources with different values, whichever runs last wins (normal
  reconciler behavior).

These specs are kept out of `examples/` precisely because they are test
inputs: `examples/` is user-facing documentation.
