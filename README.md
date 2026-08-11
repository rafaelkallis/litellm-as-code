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
| `users` | `/user/*` |
| `teams` + `members_with_roles` | `/team/*`, `/team/member_*` |
| `virtual_keys` | `/key/*` |
| `credentials` | `/credentials*` |
| `models` | `/model/*` |

Out of scope (startup-only, applied at proxy boot, **not** directly
reconciliable over the admin API): `general_settings`, `litellm_settings`,
`router_settings` from the proxy's `config.yaml`. A `config:` section in the
spec is accepted for reference and ignored.

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
the LiteLLM-ecosystem convention. See `AGENTS.md` for the full contributor
guide, scope boundaries, and API quirks reference.

## License

MIT. See [`LICENSE`](LICENSE).
