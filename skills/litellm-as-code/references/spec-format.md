# Spec format

The spec is a single YAML file (default `spec.yml`, override with
`LITELLM_SPEC`). It declares the **runtime (DB-backed)** state of the proxy.

## Scope notes

- The reconciler only touches DB-backed rows. A `config:` section
  (`general_settings` / `litellm_settings` / `router_settings`) is accepted
  for reference but **ignored** — those are startup-only and not
  reconciliable over the admin REST API. Any guardrails/policies declared only
  in the proxy's `config.yaml` are left untouched.
- Identity is API-derived; there is **no local state file**. The live proxy is
  the single source of truth for what exists.

## Top-level sections (reconcile order)

`budgets` → `models` → `credentials` → `organizations` (+ members) → `users` →
`teams` (+ members) → `virtual_keys` → `guardrails` → `policies`.

Models come before credentials because a credential's `model_id` must reference
an existing model.

## Per-resource shapes

### budgets

```yaml
budgets:
  - budget_id: "service-budget"
    max_budget: 25.0
    soft_budget: 20.0          # optional
    tpm_limit: 100000          # optional
    rpm_limit: 1000            # optional
    budget_duration: "30d"     # optional
```

### organizations (+ members_with_roles)

```yaml
organizations:
  - organization_id: "org-acme"
    organization_alias: "acme"          # optional
    max_budget: 500.0                   # optional
    budget_duration: "30d"              # optional
    members_with_roles:                 # optional; reconciled together
      - user_id: "username-admin"
        role: "org_admin"
```

### users

```yaml
users:
  - user_id: "username-admin"           # stable identity
    user_alias: "admin"                 # optional
    user_email: "admin@example.com"     # optional
    user_role: "proxy_admin"            # proxy_admin | proxy_admin_viewer |
                                        # internal_user | internal_user_viewer
    auto_create_key: "false"            # optional, string boolean
```

### teams (+ members_with_roles)

```yaml
teams:
  - team_id: "team-prod"                # stable identity (or team_alias fallback)
    team_alias: "production"            # optional
    organization_id: "org-acme"         # optional
    max_budget: 100.0                   # optional
    budget_duration: "30d"              # optional
    models: ["myorg/chat"]              # optional; model names the team can use
    members_with_roles:                 # optional; identity is (team_id, user_id)
      - user_id: "username-service"
        role: "user"
```

### virtual_keys

```yaml
virtual_keys:
  - key_alias: "admin-cli"              # stable identity (proxy-enforced unique)
    key: "sk-my-static-key"             # OPTIONAL — omit and LiteLLM generates;
                                        # only sent on CREATE, never on update
    user_id: "username-admin"
    team_id: "team-prod"                # optional
    models: ["myorg/chat"]              # optional
    max_budget: 10.0                    # optional
    budget_duration: "30d"              # optional
    allowed_routes: ["chat_completion"] # optional
```

> **Secrets are write-once.** If `key` is omitted, the reconciler creates a key
> and the generated value is printed once. `key` is never diffed against the
> live API (values come back absent) and is never sent on `/key/update` —
> sending it would rotate the key.

### credentials

```yaml
credentials:
  - credential_name: "my-vllm"          # stable identity (unique column)
    credential_info:                    # any shape; passed through
      custom_llm_provider: "hosted_vllm"
    credential_values:                  # WRITE-ONCE — never diffed against read
      api_base: "http://my-vllm:8000/v1"
      api_key: "sk-provider-key"
```

> `credential_values` come back masked from the API (`_get_masked_values`) and
> are never treated as desired state or diffed. They are re-asserted only via
> an idempotent PATCH when a **comparable** change already triggered an update.

### models

```yaml
models:
  - model_name: "myorg/chat"            # stable identity
    model_info:
      id: "11111111-2222-3333-4444-555555555555"   # optional
      mode: "chat"                      # chat | embedding | ...
      base_model: "some-chat-model"
      tier: "default"                   # optional
      input_cost_per_million_tokens: 3.0   # ecosystem convention
      output_cost_per_million_tokens: 15.0
    litellm_params:
      model: "hosted_vllm/some-chat-model"
      litellm_credential_name: "my-vllm"
```

> **Cost conversion.** The spec uses `input_cost_per_million_tokens` /
> `output_cost_per_million_tokens`. The API wants **per-token**: the
> reconciler divides by 1e6 when sending and multiplies back when comparing.
> Do the same in anything you write against the spec.

### guardrails

```yaml
guardrails:
  - guardrail_name: "pii-guard"         # stable identity
    litellm_params:
      guardrail: "presidio"
      mode: "pre_call"
    guardrail_info:                     # any shape; optional
      description: "PII masking"
```

### policies

```yaml
policies:
  - policy_name: "global-baseline"      # stable identity
    inherit: "some-base-policy"         # optional
    description: "Base guardrails for all requests"
    guardrails_add: ["pii-guard"]
```

## Validation behavior

- `load_spec` validates **before** any API call (declarative, Pydantic).
- Required identity fields are enforced, and **all errors** are collected in
  one pass (no fail-fast) — fix every reported issue before applying.
- Unknown **top-level** sections are a hard error.
- Unknown **per-resource** keys are non-fatal warnings; entries pass through
  verbatim (nested opaque payloads like `credential_values`, `litellm_params`,
  `model_info` are deliberately un-schematized).
