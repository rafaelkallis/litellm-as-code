# Upstream engagement ledger (BerriAI/litellm)

Prepared filings for the upstream breaks we absorb (issue #30). This file is
the waiting room: each entry below is a ready-to-paste issue body for
[BerriAI/litellm](https://github.com/BerriAI/litellm). **Filing is deferred by
decision (2026-10-14)** — "put them in a MD and we deal with them another
time" — which is the recorded reason required by issue #30's acceptance
criterion ("filed and cross-linked, or closed wontfix with a recorded
reason"). When filed, replace the status cell with the upstream issue URL and
cross-link it from `COMPAT.md` (the caveat row that cites it).

| # | Filing | Audience impact | Status |
|---|--------|-----------------|--------|
| 1 | Per-deployment cost persistence silently dropped (>= 1.102) | every declarative consumer of `/model/info` | prepared (pending) |
| 2 | `/key/list` pagination ignores `page_size`, uses `size` | everyone paging `/key/list` | prepared (pending) |
| 3 | Enterprise gating of `/organization/*` undocumented at the endpoint level | premium-vs-OSS consumers probing feature existence | prepared (pending) |
| 4 | `/organization/member_add` upserts a ghost user row for unknown users | any consumer syncing memberships | prepared (pending) |

---

## 1. Per-deployment cost persistence silently dropped (>= 1.102)

**Title:** Per-deployment costs silently not persisted since 1.102 (`/model/info` reads them back as null) — intentional long-term?

**Body:**

```text
Observed on 1.104.2 (and pinned on the >= 1.102 behavior by our capability
hints); 1.97.0 round-trips the same values fine.

What we see: values sent to POST /model/new and PATCH /model/{id}/update for
input_cost_per_token / output_cost_per_token are accepted but NOT persisted
since 1.102.0 (looks like 76cb0fec1 "stop persisting cost map pricing as a
deployment override" plus the c55a0a5/42541a9 echo-drops). /model/info reads
them back as null even immediately after the write, so per-deployment
pricing is effectively server-owned now (derived from the proxy's cost map).

Questions:

1. Is this an intentional long-term change (per-deployment cost overrides
   are deprecated) or an accidental regression of the 1.102 cost-map
   refactor? The lack of any changelog/deprecation signal made us trip.
2. If intentional, is there a sanctioned way to pin per-deployment costs
   for a deployment (e.g. via litellm_params, model_info, or a cost-map
   entry) that we should migrate to?
3. If accidental, was the read-back nulling intended too?

Why it matters to us: litellm-as-code diffs /model/info against a
declarative spec, so this change turned cost drift into a perpetual update
— writes that never stick. We absorb it by classifying the cost keys as
server-owned when the hint suggests >= 1.102 (and by an apply-then-verify
fallback otherwise), but a documented answer (yes/no + sanctioned
alternative) would help every Terraform-provider-style consumer, not just
us.
```

**Status:** prepared (pending). Context on our side: `litellm_as_code/capabilities.py`
(`_COSTS_PERSIST_BELOW`), `litellm_as_code/resources/models.py`
(`SERVER_DERIVED_PRICING`), `COMPAT.md` costs row, upstream commit 76cb0fec1.

## 2. `/key/list` pagination ignores `page_size`, uses `size`

**Title:** `/key/list` reads the page-size param as `size`, not its documented `page_size` (and silently defaults to the smallest page)

**Body:**

```text
Observed on every version we probed (1.97.0 and 1.104.2).

GET /key/list ignores a `page_size` query parameter entirely; it reads
`size` instead. Sending the documented param means you silently get the
smallest page (10/25) while believing you requested the documented maximum —
no error, no hint, just an under-sized result set that looks complete to a
client that stops at "fewer than page_size items".

We work around it by sending `size` for /key/list only (other list
endpoints honor page_size) — litellm-as-code pins the param name per
endpoint.

Asks (pick the cheapest):

1. honor `page_size` on /key/list like every other list endpoint;
2. or update the OpenAPI doc for /key/list to declare `size` and drop
   `page_size`, so consumers don't send a param that has no effect;
3. or at minimum 400 (reject) when the documented param is used and it
   cannot be honored — silent under-paging is the worst outcome.

Repro: GET /key/list?page_size=100 returns <=25 rows on a proxy with more
keys; GET /key/list?size=100 returns up to 100.

(Keep it small and concrete at filing time: the param mismatch, the
silent-smallest-page behavior, and the workaround. The repro line above is
good enough on its own.)

**Status:** prepared (pending). Context: `litellm_as_code/api.py`
("ignores a page_size query param entirely") and
`tests/test_api_pagination.py`.

## 3. Enterprise gating of `/organization/*` undocumented at the endpoint level

**Title:** `/organization/*` returns 403 (enterprise gate) on unlicensed proxies >= 1.102 — undocument the surface at OpenAPI/endpoint level

**Body:**

```text
Since 1.102 (unlicensed proxies), every /organization/* call answers
HTTP 403 with a body like "Organizations are a premium feature...".
Discoverable in the 403 — fine — but the OpenAPI doc still advertises the
endpoints identically on an unlicensed proxy, so a consumer cannot know the
router is gated (even asking "does this build support org management?"
requires an error-shaped probe).

Asks (pick the cheapest):

1. a `403` response documented per /organization/* path (OpenAPI list
   additions would do);
2. or an exposed capability/env flag (e.g. under /health or the version
   probe) that reports whether org endpoints are available, so consumers
   don't have to burn a real request and pattern-match the error body;
3. or a documented marker string we can rely on staying stable
   ("Organizations", "premium", "enterprise") — we match on ("403",
   "enterprise") today plus the premium wording.

Environment detail: 1.97.0 exposes /organization/* without a license
(gate appeared with the 1.102 router enterprise-ization); 1.104.2
unlicensed 403s.

Why it matters: litellm-as-code decides ORGANIZATION support per run from
this gate (declared organizations on a gated proxy hard-error, export
degrades with a notice). We don't ask for a revert — a visible contract for
the gate's surface is all a declarative consumer needs.
```

**Status:** prepared (pending). Context: `litellm_as_code/capabilities.py`
(`ORG_GATE_MARKERS`, `is_org_gate_error`), `COMPAT.md` org-gate row.

## 4. `/organization/member_add` upserts a ghost user row for unknown users

**Title:** `/organization/member_add` silently upserts a (ghost) user row when the user_id is unknown

**Body:**

```text
Verified live on 1.97.0 (unlicensed, database-backed). Behavior on later
versions is presumed unchanged — worth re-verifying on latest before or at
filing time.

What happens: POST /organization/member_add with a user_id that does NOT
correspond to any existing user row does not reject — it UPSERTS a user row
transparently (an unmanaged "ghost": no user_email/no user_alias unless the
caller passed them, role set from the call's user_role field). There is no
error and no signal in the response that a user was created as a side
effect.

Problems this causes:

- membership errors on unknown users surface much later (you see a member
  whose user row you never minted semantically);
- two writers can create distinct ghost rows for the same logical user and
  diverge (the assign vs the user manager);
- consumers that sync users separately (like a declarative reconciler)
  cannot tell "server mints the row" from "I declared the row".

Expected behavior (any of): reject unknown user_ids with 404/relationship
error; or document + return the created user row in the response; or gate
the upsert behind an opt-in flag.

Our workaround (documented in litellm-as-code): reconcile USERS before ORG
MEMBERS so the call always attaches to a row the run owns and just wrote —
the spec-declared row stays authoritative and convergence is
version-independent. But the surprise semantics are upstream's to document.
```

**Status:** prepared (pending). Context: issue
rafaelkallis/litellm-as-code#12, `litellm_as_code/reconciler.py` stage-order
docstring ("UPSERTS an unmanaged ghost user row"), `COMPAT.md` ghost-upsert
row.
