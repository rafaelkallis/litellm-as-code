# LiteLLM proxy compatibility (COMPAT.md)

[![LiteLLM nightly compat canary](https://github.com/rafaelkallis/litellm-as-code/actions/workflows/compat-nightly.yml/badge.svg)](https://github.com/rafaelkallis/litellm-as-code/actions/workflows/compat-nightly.yml)

The single answer to *"can I run `litellm-as-code` against proxy vX?"* —
citing evidence, not lore (issue #30). Everything dated here is **checked
content**: derived from or verified against repo state on every test run
(`tests/test_compat_doc.py`), so a stale doc fails CI instead of shipping.

## Tiers

| tier | meaning |
|---|---|
| **floor** | the pin the release gates (`publish-pypi.yml` / `publish-image.yml`) test against. A twofold role: the **publish gate** (a red floor leg blocks a release) and the nightly canary's **left leg**. Bumping it is a deliberate PR (AGENTS.md §9). |
| **verified** | versions the compat harness has run the live suite against and passed: the PR/push **matrix legs**, and the nightly **canary** (today's latest stable). |
| **unlisted** | anything else. Assume the admin-API contract of the nearest verified minor — upstream ships a new minor every ~6–7 days and did not earn our guarantees. Structural consumed-surface moves are caught by the nightly's [OpenAPI digest](https://github.com/rafaelkallis/litellm-as-code/actions/workflows/compat-nightly.yml) diff; behavioral ones by the canary. Run the matrix yourself and report reds. |

<!-- BEGIN GENERATED: verified — scripts/compat_doc.py; do not edit -->
Verified as of **2026-10-15** — the nightly canary ran floor `v1.97.0` and latest stable `v1.105.1` green: [38709877803](https://github.com/rafaelkallis/litellm-as-code/actions/runs/38709877803)

| tier | LiteLLM | what verifies it | where to check |
|---|---|---|---|
| floor | `v1.97.0` | publish gates (PyPI + GHCR), nightly floor, first matrix leg | green on the linked run above (2026-10-15) |
| matrix leg | `v1.97.0` | PR/push compat matrix (`.github/workflows/compat-matrix.yml`), per-leg digest artifact | status of the latest run on the [workflow page](https://github.com/rafaelkallis/litellm-as-code/actions/workflows/compat-matrix.yml) |
| matrix leg | `v1.104.2` | PR/push compat matrix (`.github/workflows/compat-matrix.yml`), per-leg digest artifact | status of the latest run on the [workflow page](https://github.com/rafaelkallis/litellm-as-code/actions/workflows/compat-matrix.yml) |
| canary | `v1.105.1` | nightly canary leg — latest stable at 04:00 UTC, resolved from `releases/latest` | green 2026-10-15 (run linked above) |
| unlisted | any other version | assume the admin-API contract of the nearest verified minor above (the consumed-surface digest watches structural moves); run the matrix yourself — `scripts/compat_matrix.py --versions vX.Y.Z` — and [report reds](https://github.com/rafaelkallis/litellm-as-code/issues/new) | — |
<!-- END GENERATED: verified -->

## Caveats: what upstream changed, and how it is absorbed

These are the reasons a *listed* version can still behave differently than a
naive de-referencing of the admin REST docs suggests. Each row is absorbed in
code (not worked around by hand-editing), self-correcting per run, and
evidence-linked to the file that owns the semantics.

<!-- BEGIN GENERATED: caveats — scripts/compat_doc.py; do not edit -->
| caveat | affects | behavior | absorbed as | evidence |
|---|---|---|---|---|
| **Per-deployment cost persistence dropped (>= 1.102)** | LiteLLM >= 1.102.0. Floor v1.97.0 (and < 1.102) persists. | `input_cost_per_token` / `output_cost_per_token` sent to `POST /model/new` and `PATCH /model/{id}/update` are accepted but not persisted; `/model/info` reads them back as null (pricing is server-derived from the proxy's cost map). | When the `model_costs_persist` hint announces >= 1.102 the two cost keys are declared server-owned for the run (`SERVER_DERIVED_PRICING`): cost-only drift becomes an accepted NOOP, mixed cost+real drift keeps its writable part. Probe unknown -> apply-then-verify fallback. | [`litellm_as_code/capabilities.py` → `_COSTS_PERSIST_BELOW = (1, 102, 0)`](litellm_as_code/capabilities.py) · [`litellm_as_code/resources/models.py` → `SERVER_DERIVED_PRICING — distinguishing marker`](litellm_as_code/resources/models.py) · [`litellm_as_code/classification.py` → `def classify_survivors(`](litellm_as_code/classification.py) · [capabilities 76cb0fec1](https://github.com/BerriAI/litellm/commit/76cb0fec1) · [#27](https://github.com/rafaelkallis/litellm-as-code/issues/27) · [PR #34](https://github.com/rafaelkallis/litellm-as-code/pull/34) · [PR #36](https://github.com/rafaelkallis/litellm-as-code/pull/36) |
| **/organization/* enterprise gate (>= 1.102, unlicensed)** | >= 1.102 on unlicensed proxies (403). Floor v1.97.0 ungated; a LICENSED >= 1.102 proxy exposes /organization. | Every `/organization/*` call answers HTTP 403 with an enterprise/premium body; the OpenAPI doc still advertises the router, so the gate is only discoverable by probing. | Behavior-probed on the first org call (`ORG_GATE_MARKERS` = 403 + enterprise body): export degrades with a visible notice; apply hard-errors only when the spec declares organizations. | [`litellm_as_code/capabilities.py` → `ORG_GATE_MARKERS = ("403", "enterprise")`](litellm_as_code/capabilities.py) · [PR #24](https://github.com/rafaelkallis/litellm-as-code/pull/24) · [#27](https://github.com/rafaelkallis/litellm-as-code/issues/27) |
| **Team member role `admin` is enterprise-only** | OSS/self-hosted unlicensed proxies of every probed generation (v1.97.0 and up). | Assigning team admins is a premium feature — role `admin` in `members_with_roles` is rejected. | OSS-convergent fixtures declare role `user`; variant A/B/C are opt-in on licensed proxies and the matrix documents the skips. | [`tests/live/README.md` → `Assigning team admins is a premium feature`](tests/live/README.md) |
| **/organization/member_add upserts a ghost user row** | v1.97.0 (probed live); presumed on later generations. | Adding an organization member for an unknown user_id does not reject — the proxy upserts an unmanaged user row (an alias/email-less ghost, role from the call). | The Users stage runs before org members, so a membership always attaches to a row the run owns and just wrote — the spec-declared row stays authoritative (version-independent). | [`litellm_as_code/reconciler.py` → `UPSERTS an unmanaged ghost user row`](litellm_as_code/reconciler.py) · [#12](https://github.com/rafaelkallis/litellm-as-code/issues/12) |
| **`POST /organization/new` needs >= 1 configured model** | v1.97.0 (probed live on a fresh proxy). | Creating an organization fails ("No models configured") until at least one model exists on the proxy. | Stage order: models -> credentials -> organizations, so a spec that declares both converges on a fresh proxy. | [`litellm_as_code/reconciler.py` → `at least one model configured`](litellm_as_code/reconciler.py) · [#23](https://github.com/rafaelkallis/litellm-as-code/issues/23) |
| **Server-injected defaults/metadata on reads** | Observed on v1.104.2 matrix legs (PR #24); present across generations in various fields. | Reads inject state the operator never declared: a `server-internal default_user_id row`, `budget_duration`, `mode` inference, tpm/rpm metadata interns (`tpm_limit_type`/`rpm_limit_type`). | Per-resource COMPARABLE lists exclude them; the exporter drops the server-internal default_user_id row; member defaults are realigned on both compare sides. | [`litellm_as_code/diff.py` → `server defaults & runtime metrics`](litellm_as_code/diff.py) · [`litellm_as_code/exporter.py` → `server-internal default_user_id row`](litellm_as_code/exporter.py) · [`litellm_as_code/resources/roles.py` → `Only fields the server actually echoes`](litellm_as_code/resources/roles.py) · [PR #24](https://github.com/rafaelkallis/litellm-as-code/pull/24) |
| **Unset limit scalars read back explicit null** | v1.97.0 (live-verified); fixture-replayed on v1.104.2. | Every unset budget-table limit and team/key `max_budget` reads back as explicit `null` — never `0`, never absent. | No `None == 0.0` tolerance exists (`equiv` adds empty-collection tolerance only); diffing tolerates the nulls as-is. | [`litellm_as_code/diff.py` → `Deliberately no scalar-default tolerance`](litellm_as_code/diff.py) · [#12](https://github.com/rafaelkallis/litellm-as-code/issues/12) |
| **`/key/list` pagination ignores `page_size`, uses `size`** | Upstream bug; observed on v1.97.0 and v1.104.2. | A `page_size` query param is ignored entirely (smallest page silently served); the endpoint reads `size` instead. | The client pins the page-size param per endpoint: `size` on `/key/list`, the documented `page_size` everywhere else. | [`litellm_as_code/api.py` → `ignores a page_size query param entirely`](litellm_as_code/api.py) |
| **Member roles always echo; defaults realigned** | v1.97.0 (live-verified, roles.py scope note). | Membership roles echo back verbatim (team `role`, org `user_role`); a diff on the raw echoes would pin the server's own default. | The default role is resolved and aligned on BOTH compare sides so the realignment cannot erase a genuine drifted value. | [`litellm_as_code/resources/roles.py` → `Only fields the server actually echoes`](litellm_as_code/resources/roles.py) · [#12](https://github.com/rafaelkallis/litellm-as-code/issues/12) |
| **`/model/info` cost echo unit variance on DB-backed models** | All probed generations with `store_model_in_db: true`. | Costs echo in two units: a recomputed per-token value (often 0 when the cost table is not loaded) plus the authoritative per-million DB columns. | Model costs compare per-token against per-million/1e6, with numeric tolerance (`0 == 0.0`, int/float); both units count as an equal echo. | [`litellm_as_code/resources/models.py` → `def _per_token_equal(`](litellm_as_code/resources/models.py) · [#24](https://github.com/rafaelkallis/litellm-as-code/pull/24) |
<!-- END GENERATED: caveats -->

## Upstream engagement

The breaks above (and the pagination bug) have **prepared upstream filings**
for [BerriAI/litellm](https://github.com/BerriAI/litellm) in
[FILINGS.md](FILINGS.md) — deliberately pending as of 2026-10-14 (recorded
decision; issue #30's "recorded reason" clause). Each caveats row affected by
one of those filings is cross-linked here.

**Ecosystem cross-check:** [`ncecere/terraform-provider-litellm`](https://github.com/ncecere/terraform-provider-litellm)
documents the same admin REST endpoints — their provider code doubles as a
seismograph for upstream breaks (and ours for theirs). When a caveat above
surprises you, check whether their provider has already absorbed it.

## Maintenance

```bash
# after changing the caveats ledger (scripts/compat_doc.py) or a workflow pin:
python scripts/compat_doc.py build      # regenerates the blocks + lints

# nightly canary greens refresh the committed sidecar AND this file in the
# same commit (see .github/workflows/compat-nightly.yml); any other drift
# (a rendered block no longer matching the render) fails
# tests/test_compat_doc.py with the exact fix above.
```

- The verified block derives from `tests/live/compat/openapi-last-known-good.meta.json`
  (nightly-maintained). The floor/legs cross-check the four workflows.
- A caveat's evidence cell must keep resolving: `file → marker` pairs point
  at the literals that own each semantics. If a refactor moves one, either
  re-point the ledger or re-word the source with the marker back in place.
- Floor bumps re-mark a generation verified — deliberate PR, never cron
  (AGENTS.md §9).
