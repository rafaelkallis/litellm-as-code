# Docker Compose example — LiteLLM + litellm-as-code

Brings up a **real, self-contained** LiteLLM stack with a local inference
server, and configures the proxy's **runtime state** from a declarative spec —
no external model provider keys required.

```
┌────────────────────────────────────────────────────────────────────┐
│  compose.yml                                                        │
│                                                                     │
│  ┌─────────────┐   /user,/team,/key,/credentials,/model,...        │
│  │   litellm   │◀─────────────────────────┐   (admin REST API)      │
│  │ :4000       │                          │                        │
│  └──────┬──────┘                          │                        │
│         │  realizes model "tiny-stories"  │                        │
│         ▼                                 │                        │
│  ┌─────────────┐   /v1/chat + /v1/models   │   one-shot             │
│  │    vllm     │◀────────┐                │  ┌──────────────┐      │
│  │ :8000       │         │  spec.yml ─────┼──▶│    config    │      │
│  │ TinyStories │         │                │  │  (reconciler) │      │
│  └─────────────┘         │                │  └──────────────┘      │
└──────────────────────────┴────────────────┴────────────────────────┘
```

Services (all on one Docker network):

| service | image | role |
|---|---|---|
| `litellm` | `ghcr.io/berriai/litellm-database` | LiteLLM proxy + admin REST API, port 4000 |
| `vllm` | `vllm/vllm-openai-cpu` | Local OpenAI-compatible inference server serving `roneneldan/TinyStories-1M`, port 8000 |
| `config` | `ghcr.io/rafaelkallis/litellm-as-code` | Run-once reconciler: diff `spec.yml` → live admin API and apply deltas |

## Why TinyStories-1M?

[`roneneldan/TinyStories-1M`](https://huggingface.co/roneneldan/TinyStories-1M)
is a ~3.7M-parameter GPT-Neo/GPT-2-class model from the TinyStories paper. It's
tiny enough to run on **CPU**, so the example needs no GPU and no external API
keys. It's still a real model — you can `curl` the proxy and get an actual
story out of the other end.

## Prerequisites

- Docker (with Docker Compose v2)
- A machine with a reasonable amount of RAM (the proxy + vLLM CPU + model
  weights fit comfortably in a few GB)
- Network access the first run (to pull images + download the model weights
  from Hugging Face)

> **CPU arch note:** the compose file defaults `VLLM_ARCH=latest-x86_64`.
> On Apple Silicon set `VLLM_ARCH=latest-arm64`. vLLM's CPU backend prefers
> AVX-512; on plain AVX2 CPUs it falls back to "limited features", which is
> fine at this model size.

## Run it

```bash
cd examples/docker-compose
cp .env.example .env          # then set LITELLM_MASTER_KEY (and salt) in .env
docker compose up -d litellm vllm     # start the proxy + vLLM
docker compose run --rm config --dry-run   # plan: see what would change (exit 2 on diff)
docker compose run --rm config              # apply / converge the proxy state
```

Re-running `config` is **idempotent**: the second run reports `0 to create,
0 to update, N unchanged` and exits `0`.

## Try it out

`config` post-registers the `tiny-stories` model in LiteLLM's DB, backed by
the local vLLM deployment. Test the full path through the proxy:

```bash
# list available models through the proxy
curl -s http://localhost:4000/v1/models -H "Authorization: Bearer $LITELLM_MASTER_KEY"

# ask TinyStories for a story, through LiteLLM -> vLLM
curl -s http://localhost:4000/chat/completions \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"tiny-stories","messages":[{"role":"user","content":"Once upon a time,"}]}'
```

Or hit the raw engine directly (bypasses LiteLLM) for debugging:

```bash
curl -s http://localhost:8000/v1/models
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

- `litellm` runs SQLite (file-backed) via `litellm-database`, with
  `STORE_MODEL_IN_DB=True` so models can be managed through the admin API
  (required by the reconciler).
- `vllm` serves `roneneldan/TinyStories-1M` with CPU-friendly flags
  (`--dtype bfloat16`, small KV cache, eager mode).
- `config` waits for `litellm` to be healthy, then reconciles. Because models
  are DB-backed, there's no need to list them in the startup `config.yaml`.

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
- If vLLM fails to download the weights (network), the `vllm` service will
  not become healthy and `config` will wait. Check `docker compose logs vllm`.

## What was verified without Docker

This repo's CI/dev environment is Docker-free, so the full stack was **not**
booted here. What *is* verified, against the in-repo test harness:

- `spec.yml` in this directory passes the project's own `load_spec` +
  `validate_spec` (0 errors, 0 warnings).
- Running the actual reconciler against the in-memory fake proxy: first run
  creates all 9 sections, re-run is a **no-op** (idempotent, keys not
  rotated), and `--dry-run` reports the creates without mutating.
- The model is registered with the expected routing (credential `tiny-vllm`
  → `http://vllm:8000/v1`, `model` = `hosted_vllm/roneneldan/TinyStories-1M`).

To see the stack live, run `docker compose up` on a machine with Docker (you'll
need network egress for first-time image pulls and the Hugging Face model
download).
