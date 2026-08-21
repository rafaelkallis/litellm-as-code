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
