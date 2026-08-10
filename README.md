# litellm-as-code

Declarative **runtime-state** management for a [LiteLLM](https://github.com/BerriAI/litellm) proxy.

Write a YAML spec describing the users, teams, virtual API keys, credentials,
and models you want running on your LiteLLM proxy — then run one command and
`litellm-as-code` makes reality match, applying only the deltas. It's the
"config as code" workflow you'd expect from Terraform, but purpose-built for
the LiteLLM admin REST API.

Key properties:

- **Declarative & idempotent** — run twice, second run is a no-op. Existing
  virtual keys are **never rotated** (see *Secrets & state*).
- **Plan before apply** — `--dry-run` prints exactly what would change and
  exits non-zero if a diff exists (CI-friendly).
- **Drift-aware** — diffs manageable fields against the live proxy, not just
  against a local file.
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
# or
docker build -t litellm-as-code .
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

Output `/state/state.json` is written by default; keep it out of version
control and on a persistent volume.

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
                       [--state FILE] [--dry-run] [--prune] [spec]

positional:
  spec                   path to YAML spec (env: LITELLM_SPEC, default spec.yml)

options:
  --base-url URL         LiteLLM proxy base URL (env: LITELLM_BASE_URL / BASE_URL)
  --api-key KEY          admin API key (env: LITELLM_API_KEY / API_KEY)
  --state FILE           applied-state file (env: LITELLM_STATE, default state.json)
  --dry-run              print changes without applying; exit 2 if any
  --prune                (reserved) delete live resources absent from spec
```

Exit codes: `0` clean/no-op · `1` error · `2` diff present (`--dry-run` only).

## Docker

```bash
# persist applied-state across runs:
docker run --rm \
  -e LITELLM_BASE_URL="http://proxy:4000" \
  -e LITELLM_API_KEY="sk-admin-..." \
  -v "$PWD/spec.yml:/config/spec.yml:ro" \
  -v "$PWD/state:/state" \
  litellm-as-code --dry-run
```

The image defaults to `LITELLM_SPEC=/config/spec.yml` and
`LITELLM_STATE=/state/state.json` and runs as a non-root user.

## Secrets & applied-state (`state.json`)

LiteLLM's admin API **never returns secret material back**: `credential_values`
and a virtual key's raw `key` value are stored by the proxy but not echoed by
GET endpoints (only a hashed key name is returned). This is deliberate.

So — exactly like Terraform's `.tfstate` — `litellm-as-code` keeps a local
applied-state file. The **live API** decides drift for comparable fields; the
**state file** decides whether a secret was already sent, so existing keys are
never rotated and credentials aren't re-sent on every run.

Implications:

- Keep `state.json` private (`chmod 600`, gitignored here, on a persistent
  volume in Docker).
- If a secret is rotated out-of-band in the proxy, the state file won't notice
  (same documented limitation as Terraform's state). Delete the relevant entry
  in `state.json` to force re-apply.
- Losing the state file means keys/credentials get recreated on the next apply.

## How it works

```
spec.yml ──diff──▶ live API + state.json ──apply──▶ converge
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

```bash
pip install -e ".[dev]"
pytest
```

Tests are mock-only (in-memory fake proxy) and mirror the package layout, per
the LiteLLM-ecosystem convention. See `AGENTS.md` for the full contributor
guide, scope boundaries, and API quirks reference.

## License

MIT. See [`LICENSE`](LICENSE).
