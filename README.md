# litellm-as-code

Declarative **runtime-state** management for a [LiteLLM](https://github.com/BerriAI/litellm) proxy.

Write a YAML spec describing the users, teams, virtual API keys, credentials,
and models you want running on your LiteLLM proxy — then run one command and
`litellm-as-code` makes reality match, applying only the deltas. It's the
"config as code" workflow you'd expect from Terraform, but purpose-built for
the LiteLLM admin REST API.

Key properties:

- **Declarative & idempotent** — run twice, second run is a no-op. Existing
  virtual keys are **never rotated** (see *Secrets*).
- **Plan before apply** — `--dry-run` prints exactly what would change and
  exits non-zero if a diff exists (CI-friendly).
- **Drift-aware** — identity & drift come entirely from the **live proxy**
  (no local state file); comparable fields are diffed against the API.
- **Lightweight** — a single Python CLI or container; no Terraform, no graph
  engine.

## Scope

`litellm-as-code` manages **runtime (DB-backed) state** of a LiteLLM proxy:

| Spec section | Managed resource |
|---|---|
| `budgets` | `/budget/*` |
| `organizations` (+ `members_with_roles`) | `/organization/*`, `/organization/member_*` |
| `users` | `/user/*` |
| `teams` + `members_with_roles` | `/team/*`, `/team/member_*` |
| `virtual_keys` | `/key/*` |
| `credentials` | `/credentials*` |
| `models` | `/model/*` |
| `guardrails` | `/guardrails*` |
| `policies` | `/policies*` |

Out of scope (startup-only, applied at proxy boot, **not** directly
reconciliable over the admin API): `general_settings`, `litellm_settings`,
`router_settings` from the proxy's `config.yaml`. A `config:` section in the
spec is accepted for reference and ignored. The same applies to profile
sections that only exist in `config.yaml` (e.g. guardrails/policies defined
there); the reconciler only touches DB-backed rows and never deletes
config-file-only entries.

## Requirements

- Python 3.10+ (or Docker)
- LiteLLM proxy with `store_model_in_db: true` (DB-backed models)
- An admin API key

## Install

```bash
pip install .            # from source
# or use the prebuilt image (see Docker below):
docker pull ghcr.io/rafaelkallis/litellm-as-code:latest
```

(Or copy `examples/spec.yml` and run from a venv: `pip install -e .`)

## Quickstart

```bash
# 1. point at your proxy
export LITELLM_BASE_URL="http://your-proxy:4000"
export LITELLM_API_KEY="sk-admin-..."

# 2. write a spec (see examples/spec.yml)
cp examples/spec.yml spec.yml

# 3. plan (no changes applied; exit 2 if a diff exists)
litellm-as-code spec.yml --dry-run

# 4. apply
litellm-as-code spec.yml
```

No local state file is written — the live proxy is the single source of truth
for what exists and what drifted.

## Example spec

See [`examples/spec.yml`](examples/spec.yml) for the full shape. Bare sketch:

```yaml
budgets:
  - budget_id: "platform-budget"
    max_budget: 100.0
    budget_duration: "30d"

organizations:
  - organization_id: "org-acme"
    organization_alias: "acme"
    members_with_roles:
      - user_id: "username-admin"
        role: "org_admin"

users:
  - user_id: "username-admin"
    user_alias: "admin"
    user_role: "proxy_admin"
    auto_create_key: "false"

teams:
  - team_id: "team-prod"
    team_alias: "production"
    members_with_roles:
      - user_id: "username-admin"
        role: "admin"

virtual_keys:
  - key_alias: "admin-cli"
    key: "sk-my-static-key"     # optional; omitted => LiteLLM generates
    user_id: "username-admin"

credentials:
  - credential_name: "my-vllm"
    credential_info: { custom_llm_provider: "hosted_vllm" }
    credential_values:
      api_base: "http://my-vllm:8000/v1"
      api_key: "..."

models:
  - model_name: "myorg/chat"
    model_info:
      id: "11111111-2222-3333-4444-555555555555"
      mode: "chat"
      base_model: "some-chat-model"
      input_cost_per_million_tokens: 3.0
    litellm_params:
      model: "hosted_vllm/some-chat-model"
      litellm_credential_name: "my-vllm"

guardrails:
  - guardrail_name: "pii-guard"
    litellm_params:
      guardrail: "presidio"
      mode: "pre_call"
    guardrail_info:
      description: "PII masking"

policies:
  - policy_name: "global-baseline"
    description: "Base guardrails for all requests"
    guardrails_add: ["pii-guard"]
```

## CLI

```
usage: litellm-as-code [--version] [--base-url URL] [--api-key KEY]
                       [--dry-run] [--prune] [spec]

positional:
  spec                   path to YAML spec (env: LITELLM_SPEC, default spec.yml)

options:
  --base-url URL         LiteLLM proxy base URL (env: LITELLM_BASE_URL / BASE_URL)
  --api-key KEY          admin API key (env: LITELLM_API_KEY / API_KEY)
  --dry-run              print changes without applying; exit 2 if any
  --prune                (reserved) delete live resources absent from spec
```

Exit codes: `0` clean/no-op · `1` error · `2` diff present (`--dry-run` only).

## Docker

Images are published to **GitHub Container Registry**, tagged with each release
version and `latest` (multi-arch `linux/amd64` + `linux/arm64`):

```bash
docker run --rm \
  -e LITELLM_BASE_URL="http://proxy:4000" \
  -e LITELLM_API_KEY="sk-admin-..." \
  -v "$PWD/spec.yml:/config/spec.yml:ro" \
  ghcr.io/rafaelkallis/litellm-as-code:latest --dry-run
```

Or build from source with `docker build -t litellm-as-code .` and use
`litellm-as-code` in place of the image name above.

The image defaults to `LITELLM_SPEC=/config/spec.yml` and runs as a non-root
user. No secrets are persisted by the tool (the spec mounts its own credentials).

### Docker Compose example (LiteLLM + litellm-as-code end-to-end)

Want to see it all working together before wiring up a real proxy? There's a
runnable example under [`examples/docker-compose/`](examples/docker-compose/)
that boots a LiteLLM proxy with `docker compose up` and configures its runtime
state from a declarative spec. The proxy **upstreams to an externally hosted,
OpenAI-compatible service** (hosted vLLM, an OpenAI-compatible gateway, etc.) —
you only supply the base URL + API key; there's no model self-hosting here:

- **`postgres`** — the DB backing the proxy's runtime state (required by
  LiteLLM's DB-backed features);
- **`litellm`** — the proxy (`ghcr.io/berriai/litellm-database`,
  Postgres-backed, DB-managed models);
- **`config`** — the `litellm-as-code` reconciler as a run-once job that
  registers the external model + resources from
  [`spec.yml`](examples/docker-compose/spec.yml) against the live
  admin API (and re-runs cleanly/idempotently).

```bash
cd examples/docker-compose
cp .env.example .env        # set LITELLM_MASTER_KEY
docker compose up -d postgres litellm           # start DB + proxy
docker compose run --rm config --dry-run        # plan (exit 2 on diff)
docker compose run --rm config                  # apply (registers resources + model)
curl -s http://localhost:4000/v1/models -H "Authorization: Bearer $LITELLM_MASTER_KEY"
```

See [`examples/docker-compose/README.md`](examples/docker-compose/README.md)
for the full walkthrough (including an interactive `curl` through the proxy to
the locally-served model).

## Secrets

LiteLLM's admin API **never returns secret material back**: `credential_values`
are masked on every read and a virtual key's raw `key` value is never echoed.
This is deliberate, so `litellm-as-code` has **no local state file**:

- **Keys**: the spec's `key` is sent exactly once, on create (`/key/generate`),
  if present; otherwise LiteLLM mints one. Updates only touch non-secret fields
  and never re-send `key`, so existing keys are **never rotated**.
- **Credentials**: `credential_values` are sent on create and re-asserted only
  when a comparable (`credential_info` / `model_id`) change already triggered a
  PATCH. PATCH is idempotent and does not rotate.

Because secrets are not diffed against live, a secret rotated out-of-band in the
proxy is invisible to the tool (same documented limitation as Terraform, without
the state file): change the value in the spec and delete/recreate the resource
to force a new secret.

## How it works

```
spec.yml ──diff──▶ live API ──apply──▶ converge
```

Order is fixed: `users → teams → team members → keys → credentials → models`
(acyclic, single-target). Per resource: derive identity, diff COMPARABLE
fields against the live object, then `create`/`update` only on change.

LiteLLM API quirks handled internally: nested read envelopes
(`/key/info` → `info`, `/user/info` → `user_info`, `/team/info` →
`team_info`, model list → `data`), server-injected defaults & read-only
runtime metrics (never diffed), and exponential-backoff retries for
read-after-create eventual consistency.

## Spec validation

Specs are validated **before any API call** (inside `load_spec`, so even
`--dry-run` rejects a malformed spec). Validation is declarative and
per-resource (Pydantic models in `litellm_as_code/validation.py`):

- **Required identity fields** per resource (`user_id`, `key_alias`,
  `credential_name`, `model_name`, `guardrail_name`, `policy_name`,
  `budget_id`, and at least one of `team_id`/`team_alias` or
  `organization_id`/`organization_alias`).
- **Types & enums** for the manageable fields (budget limits as numbers,
  `user_role` and member `role` as allowed enum values, model/route lists,
  etc.). The `key` value is only checked as a non-empty string when present
  — it is write-once and never diffed.
- **All errors are collected and reported in one pass** — you see every
  problem in the spec at once (no fail-fast), and the CLI exits `1`.
- **Unknown top-level sections are a hard error** (typo guard).
- **Unknown per-resource keys are warnings only** — LiteLLM keeps adding
  fields, so an extra key doesn't block a run; entries are passed through
  verbatim and the reconciler's `[warn]` output flags them on stderr.
  Warnings never affect the exit code.

Nested opaque payloads (`credential_values`, `litellm_params`, `model_info`,
`metadata`) are intentionally not closed schemas — providers pass arbitrary
params — so only their well-known subfields are type-checked. Cross-resource
reference checks (e.g. a key's `user_id` existing in `users`) are out of
scope for now.

## Available tools (lookup helpers)

Not yet covered; the admin list endpoints this project relies on are:
`/user/list`, `/v2/team/list`, `/key/list`, `/credentials`, `/model/info`.
Data-source lookups (single object by ID) are a candidate future addition.

## Development

The repo is managed with [uv](https://docs.astral.sh/uv/): the checked-in
`uv.lock` pins the dev environment, and the working `.venv` is uv-created
(so it intentionally has no `pip`). Use `uv` to install and run, not `pip`:

```bash
uv sync --extra dev      # create/refresh .venv with pytest + pytest-mock
uv run pytest            # or: .venv/bin/python -m pytest
```

If you don't want uv, `pip install -e ".[dev]"` into your own venv also works.

Tests are mock-only (in-memory fake proxy) and mirror the package layout, per
the LiteLLM-ecosystem convention.

### Live-proxy integration tests (optional)

`tests/live/` additionally ships an **integration suite** that runs the
reconciler against a **real LiteLLM proxy** (see
[`tests/live/README.md`](tests/live/README.md)). It is collected but skipped
by default (`-m 'not integration'` in `pyproject.toml`); run it with a proxy
up and the env vars set:

```bash
cd examples/docker-compose && docker compose up -d postgres litellm  # proxy on :4000
export LITELLM_BASE_URL=http://localhost:4000
export LITELLM_API_KEY=sk-demo-master-key-change-me   # your LITELLM_MASTER_KEY
uv run pytest tests/live -m integration -v
```

This suite is a **hard quality gate for publishing**: the `publish` job in
`.github/workflows/publish-image.yml` runs only after both the mock-only unit
suite and the live integration suite pass (variants A/C + the mutation round,
plus the documented example spec as a slow test).

See `AGENTS.md` for the full contributor guide, scope boundaries, and API
quirks reference.

## License

MIT. See [`LICENSE`](LICENSE).
