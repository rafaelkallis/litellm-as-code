"""Unit tests for drift normalization helpers (diff.py, resources/roles.py).

These capture the exact live-proxy behaviors discovered while testing against
a real LiteLLM deployment:

- `/user/list` omits `auto_create_key` / `user_email` for master-key-created
  rows, so a spec that sets them must not read as perpetual drift.
- LiteLLM always echoes list-shaped fields as (possibly empty) arrays; an
  omitted spec field (None) must compare equal to an empty list.
"""

from __future__ import annotations

from litellm_as_code.diff import comparable_diff
from litellm_as_code.resources.roles import realign


def test_realign_drops_fields_absent_from_live():
    desired = {
        "user_id": "u1",
        "user_alias": "admin",
        "user_email": "admin@example.com",
        "user_role": "proxy_admin",
        "auto_create_key": "false",
    }
    # Real /user/list payload: auto_create_key and user_email are NOT echoed.
    live = {
        "user_id": "u1",
        "user_alias": "admin",
        "user_role": "internal_user",
    }
    want = realign(desired, live, ["user_alias", "user_email", "user_role", "auto_create_key"])

    assert want == {"user_id": "u1", "user_alias": "admin", "user_role": "proxy_admin"}
    # only the echoed subset can diff
    assert comparable_diff(want, live, ["user_alias", "user_email", "user_role", "auto_create_key"]) == {
        "user_role": ("proxy_admin", "internal_user")
    }


def test_realign_keeps_fields_live_explicitly_nulls():
    desired = {"user_id": "u1", "user_email": "admin@example.com"}
    # live explicitly carries the field with a null value -> still diffed
    live = {"user_id": "u1", "user_email": None}
    want = realign(desired, live, ["user_email"])
    assert want == desired
    assert comparable_diff(want, live, ["user_email"]) == {
        "user_email": ("admin@example.com", None)
    }


def test_equiv_none_equals_empty_list_both_directions():
    # spec omits models; live returns []
    assert comparable_diff({}, {"models": []}, ["models"]) == {}
    # spec sets []; live omits
    assert comparable_diff({"models": []}, {}, ["models"]) == {}


def test_equiv_none_still_differs_from_nonempty_list():
    changes = comparable_diff({}, {"models": ["gpt-4"]}, ["models"])
    assert changes == {"models": (None, ["gpt-4"])}


def test_equiv_preserves_type_sensitive_comparison_for_scalars():
    # strings still diff on exact value
    assert comparable_diff({"user_role": "proxy_admin"}, {"user_role": "internal_user"}, ["user_role"])
    # None vs None no diff
    assert comparable_diff({"user_role": None}, {"user_role": None}, ["user_role"]) == {}
