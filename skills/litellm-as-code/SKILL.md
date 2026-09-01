---
name: litellm-as-code
description: >
  Manage the runtime state of a live LiteLLM proxy (users, teams, virtual API
  keys, credentials, models, budgets, organizations, guardrails, policies)
  declaratively using litellm-as-code. Use when the user wants to add or modify
  proxy users/keys/credentials/models, reconcile a YAML spec against a proxy,
  plan or apply changes, export an existing proxy's state into a spec, or
  understand the drift/diff model and exit codes.
license: MIT
metadata:
  author: Rafael Kallis
  source: https://github.com/rafaelkallis/litellm-as-code
  version: "0.1.0"
---

# litellm-as-code

Declarative runtime-state management for a LiteLLM proxy. You declare the
users, teams, virtual API keys, credentials, models, budgets, organizations,
guardrails, and policies you want in a YAML spec; `litellm-as-code` diffs the
spec against the live proxy and applies only the deltas.

> Scope: this skill manages **runtime (DB-backed)** state only. It does not
> manage the proxy's startup `config.yaml` (`general_settings` /
> `litellm_settings` / `router_settings`), which is applied at boot and is not
> reconciliable over the admin REST API.

## When to use

- The user asks to add, update, or remove users / teams / virtual keys /
  credentials / models / budgets / organizations / guardrails / policies on a
  LiteLLM proxy.
- The user has (or should have) a YAML spec and wants to plan or apply it.
- The user is diagnosing why the live proxy does not match a spec (drift).
- The user is writing a spec and wants the exact per-resource shape.
- The user has an existing (already configured) proxy and wants to adopt
  `litellm-as-code` — export the live state into a spec.

## How to use

1. **Get the environment.** Confirm/provide the proxy base URL and an **admin**
   API key (a virtual key scoped to `llm_api_routes` is not sufficient):
   - `LITELLM_BASE_URL` (e.g. `https://proxy.example.com:4000`)
   - `LITELLM_API_KEY` (a proxy admin key)

   Install the CLI if it is not present:

   ```bash
   uvx litellm-as-code --version      # run the latest release without installing
   pip install litellm-as-code       # or install into the current environment
   ```

   Installing this skill into your agent is a one-liner:

   ```bash
   npx skills add rafaelkallis/litellm-as-code   # or `gh skill install ...`
   ```

2. **Locate or create the spec.** The default file is `spec.yml` (or set
   `LITELLM_SPEC`). If the user is starting fresh or you need the exact shape,
   refer to [spec-format](./references/spec-format.md) and copy the starter
   template from `./templates/spec.yml`.

3. **Plan before applying (always for anything non-trivial):**

   ```bash
   litellm-as-code spec.yml --dry-run
   ```

   This prints exactly what would change and applies nothing. Note the exit
   codes (see below) — a non-zero exit on dry-run means a diff exists, which is
   CI-friendly but is **not** an error when planning. See
   [drift-and-apply](./references/drift-and-apply.md) for the full model.

4. **Apply:**

   ```bash
   litellm-as-code spec.yml
   ```

5. **Interpret the result.** Second run is a no-op (idempotent). If a resource
   was created, the reconciler prints the event and, for keys, the generated key.

### Exporting an existing proxy (adoption)

To turn a **live, already-configured** proxy into a spec:

```bash
litellm-as-code export spec.yml --base-url "$LITELLM_BASE_URL" --api-key "$LITELLM_API_KEY"
```

This is **read-only** — it never applies anything. It emits every *comparable*
field (reconcile-order sections) and **never emits secrets**:
`credential_values` come out empty with an inline `# <fill: …>` comment (fill
before applying), and `virtual_keys[].key` is omitted (re-apply mints a fresh
key). Runtime metrics and server-injected defaults never appear. The exported
file is validated by the same `load_spec` pipeline and re-applying it to the
source proxy is a no-op. See [spec-format](./references/spec-format.md).

## Key invariants to respect

- **Never read secrets back or treat them as comparable.** The API returns
  `credential_values` masked and a key's raw `key` absent. Secrets are
  **write-once**: sent on create, never diffed, and never re-asserted except
  as part of an idempotent PATCH that a comparable change already triggered.
- **Never send `key` on update** — that would rotate the key. `key` is only
  sent on create.
- **Exit codes:** `0` = clean/no diff (applied or no-op) · `1` = error ·
  `2` = changes shown by `--dry-run`.
- **Identity is API-derived** — there is no local state file. Existence is
  determined from the live proxy (`key_alias`, `credential_name`, `user_id`,
  `team_id`, `model_name`).

## CLI reference

```
litellm-as-code <spec> [--base-url URL] [--api-key KEY] [--dry-run] [--prune] [--quiet]
litellm-as-code export [OUT] [--base-url URL] [--api-key KEY] [--quiet]   # read-only
```

Every invocation prints a short **author & license notice** to stderr first
(`litellm-as-code <version> — by Rafael Kallis, licensed under MIT`). It is
stderr-only, so it never pollutes stdout (e.g. for `export`). The notice is
suppressed only for `--quiet` (scripting) and for the stdlib argparse actions
`--help`/`--version` (which print & exit before the notice would be emitted).

Env aliases: `LITELLM_SPEC` · `LITELLM_BASE_URL`/`BASE_URL` ·
`LITELLM_API_KEY`/`API_KEY`. `--prune` is reserved and effectively a no-op
today (additive reconcile only). `export` exits `0` on success, `1` on error,
and applies nothing.

## References

- [Spec format](./references/spec-format.md) — full per-section YAML shapes,
  write-once secrets, cost conversion.
- [Drift & apply](./references/drift-and-apply.md) — identity model,
  diff/apply semantics, `--dry-run`, exit codes, ordering.
- [Starter template](./templates/spec.yml) — editable starting spec.
