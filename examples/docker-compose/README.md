# Docker Compose example — LiteLLM + litellm-as-code

Brings up a LiteLLM proxy (with a Postgres DB) and configures its **runtime
state** from a declarative spec. The proxy **upstreams to an externally hosted,
OpenAI-compatible service** — we don't self-host a model — so you only need
the base URL + API key of whatever service you already have (hosted vLLM,
an OpenAI-compatible gateway, etc.).

```
             /user,/team,/key,/credentials,/model,...

        ┌─────────────────────────────┐
        │          litellm :4000       │
        │   (LiteLLM proxy + admin API)│
        └──┬───────────────────────▲──┘
           │  upstream to external │
           ▼  OpenAI-compatible    │  spec.yml
        ┌──────────────────┐       │      │
        │   external LLM   │       │   ┌──▼───────────┐
        │   (hosted vLLM / │       │   │     config    │
        │   OpenAI-compat.)│       │   │ (reconciler)  │
        └──────────────────┘       │   └──────────────┘
                                   │        (one-shot run)
                                   ▼
                            admin REST API
```

Services (all on one Docker network):

| service | image | role |
|---|---|---|
| `postgres` | `postgres:16-alpine` | DB backing the proxy's runtime state (users/teams/keys/models) |
| `litellm` | `ghcr.io/berriai/litellm-database` | LiteLLM proxy + admin REST API, port 4000 |
| `config` | `ghcr.io/rafaelkallis/litellm-as-code` | Run-once reconciler: diff `spec.yml` → live admin API and apply deltas |

The external, OpenAI-compatible upstream is **not** part of this compose file —
you supply its `base_url` + `api_key`. The exact model listed in `spec.yml`
(the `models` section) is whatever that service exposes.

## Prerequisites

- Docker (with Docker Compose v2)
- An externally hosted, OpenAI-compatible inference endpoint (base URL + API
  key + a model name)
- Network access to pull the images on first run

## Run it

```bash
cd examples/docker-compose
cp .env.example .env          # set LITELLM_MASTER_KEY (and salt) in .env
# Also fill in OPENAI_COMPATIBLE_BASE_URL / _API_KEY / _MODEL if you are not
# editing spec.yml directly.
docker compose up -d postgres litellm       # start the DB + proxy
docker compose run --rm config --dry-run     # plan: see what would change (exit 2 on diff)
docker compose run --rm config               # apply / converge the proxy state
```

Re-running `config` is **idempotent**: the second run reports `0 to create,
0 to update, N unchanged` and exits `0`.

## Point the proxy at your external service

Two ways — pick one:

1. **Let the reconciler manage it (recommended for this example).** Edit
   `spec.yml` → `credentials` and `models`:

   ```yaml
   credentials:
     - credential_name: "external-openai-compatible"
       credential_info:
         custom_llm_provider: "hosted_vllm"   # generic OpenAI-compatible
       credential_values:
         api_base: "https://your-service.example.com/v1"
         api_key: "sk-..."

   models:
     - model_name: "your-model-name"
       model_info: { mode: "completion", base_model: "your-remote-model" }
       litellm_params:
         model: "hosted_vllm/your-remote-model"
         litellm_credential_name: "external-openai-compatible"
   ```

   Then run `docker compose run --rm config` to register them in the proxy.

2. **Via the startup `config.yaml`.** If you'd rather not manage the upstream
   through the reconciler, set it there as a normal `model_list` entry and
   leave it out of `spec.yml` (the reconciler only touches DB-backed rows it's
   told about).

## Try it out

After `config` runs, the model is registered in LiteLLM's DB and upstreams to
your external service. Test the full path through the proxy:

```bash
# list available models through the proxy
curl -s http://localhost:4000/v1/models -H "Authorization: Bearer $LITELLM_MASTER_KEY"

# send a chat completion via the registered model
curl -s http://localhost:4000/chat/completions \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"your-model-name","messages":[{"role":"user","content":"Hello!"}]}'
```

LiteLLM's Admin UI is at `http://localhost:4000/ui` (log in with
`LITELLM_MASTER_KEY`).

## What `config` (litellm-as-code) manages

`spec.yml` in this directory is the source of truth for the proxy's **runtime
state**: budgets, organizations, users, teams, virtual keys, credentials,
models, guardrails, and policies. The reconciler diffs it against the live
admin REST API and applies only the deltas, in the fixed order
`budgets → organizations → users → teams → keys → credentials → models →
guardrails → policies`.

Things the reconciler deliberately does **not** manage:

- **Startup `config.yaml`** (`general_settings` / `litellm_settings` /
  `router_settings`). Those are applied at proxy boot. This example's
  `config.yaml` sets up the DB and is mounted read-only into the proxy;
  `spec.yml` may contain a reference `config:` section that is ignored.
- **Secret material** such as virtual key `key` values or
  `credential_values` — they are write-once (sent on create, never read back),
  so they can never be diffed against live.

## Under the hood

- `postgres` provides the execution database. The proxy's runtime state
  (users/teams/keys/credentials/models) is stored there, so `Store model in DB`
  (`STORE_MODEL_IN_DB=True`) works — models can be managed through the admin
  API (required by the reconciler).

  > LiteLLM requires PostgreSQL for its DB-backed features (`DATABASE_URL`)
  > and rejects `sqlite://` — this stack therefore uses a real Postgres.
- `config` waits for `litellm` to be healthy, then reconciles. Because models
  are DB-backed, there's no need to list them in the startup `config.yaml`.
- The actual LLM traffic is forwarded to your external OpenAI-compatible
  endpoint when a model is called; LiteLLM handles auth, rate-limits, and
  key/budget tracking on top.

## Notes / tips

- The reconciler is **stateless** — no local state file. The live proxy is the
  single source of truth; rerun `config` any time to detect drift.
- Want the reconciler to plan instead of apply (CI-friendly)? `--dry-run`
  exits with code `2` when a diff exists (like `terraform plan`).
- To point `config` at an *external* proxy instead of the compose one,
  override `LITELLM_BASE_URL` / `LITELLM_API_KEY`:
  ```bash
  docker compose run --rm -e LITELLM_BASE_URL=http://your-proxy:4000 config
  ```
