"""Server-echo normalization for LiteLLM read responses.

LiteLLM does NOT echo every comparable field on read for every row it emits.
Concrete cases seen against a real proxy:

- `/user/list` omits `auto_create_key` and `user_email` for rows that were
  created through the master-key (service-account) insert path and for the
  server-internal `default_user_id` row.
- `/organization/list` may omit fields a spec sets; `models` etc. are only
  echoed when the column has a value.

A naive spec-vs-live diff treats an *omitted live field* as `None`, so a spec
value like `user_email: admin@example.com` reads as perpetual
`'admin@example.com' -> None` drift and the reconciler re-updates forever.

`realign` handles this: when the live row (and its type) suggests the server
does *not manage* a field — i.e. the field is simply absent from the read
payload, not explicitly null — we drop that field from the *desired* side
before diffing. Explicit `None` in the read is respected and still diffs.
Only fields the server actually echoes take part in the comparison.

Scope (issue #12): `realign` is deliberately applied by `users.py` only — it
is the resource where whole row classes are observed to omit fields the spec
can set (`/user/list` omits `auto_create_key` / `user_email` on
master-key-created rows). Generalizing absent-in-live skipping into
`comparable_diff` would turn "server didn't echo" into "never update" for
every resource — a convergence hazard wherever LiteLLM stores-but-omits, and
per-field absence semantics are not verified for other resources against the
pinned proxy (AGENTS.md §9).

Verified against the pinned proxy (v1.97.0, probed live, issue #12): unset
budget-table limits (max_budget / soft_budget / max_parallel_requests /
tpm_limit / rpm_limit / model_max_budget / budget_duration), unset team
`max_budget` and unset key `max_budget` all read back as EXPLICIT `null` —
not `0`/`0.0` and not omitted — so `equiv`'s None-vs-value tolerance already
converges them and no scalar-default normalizer exists for budgets, teams or
keys. A `None == 0` tolerance was considered and rejected: no pinned-version
row class echoes it, and a tolerance without a verified echo class is exactly
the sensitivity loss live verification is meant to prevent. Member roles
(team `role`, org `user_role`) are echoed on every read as well; the inline
`validation.DEFAULT_*_ROLE` resolution at both comparison sides (issue #7 on
the want side, issue #12 on the live side) stays as a convergence guarantee
for the spec-omits-role shape. The remaining per-resource normalizations:

- team / org members — the shared server-default role is resolved inline at
  both comparison sides (`validation.DEFAULT_*_ROLE`), covering the spec side
  defaulting (issue #7) and guaranteeing convergence if a read ever leaves
  the role unset (issue #12).
- models — builds its own comparison map (`_comparable_model`) and compares
  costs with unit tolerance (`_per_token_equal`).
- policies — list-shaped fields are normalized to empty defaults
  (`_LIST_DEFAULTS`); empty containers are covered by `diff.equiv`.

Until a field's absent-in-live semantics are verified live, prefer one of the
narrower patterns above over widening `realign`/`equiv`.
"""

from __future__ import annotations

from typing import Any


def realign(
    desired: dict[str, Any], live: dict[str, Any], fields: list[str]
) -> dict[str, Any]:
    """Return a copy of `desired` filtered to fields the live row echoes.

    Fields absent from `live` (not explicitly present with value None) are
    removed from the returned desired dict. The result can be diffed directly
    against `live` with `comparable_diff` — removed fields can no longer read
    as drift.
    """
    out = dict(desired)
    for f in fields:
        if f not in out:
            continue
        if f not in live:
            out.pop(f)
    return out
