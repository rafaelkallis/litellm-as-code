"""Pagination tests for LiteLLMClient list endpoints (mock-only).

The client must page through /user/list, /v2/team/list and /key/list instead
of silently reading page 1. Envelope shapes mirror the live proxy (verified on
v1.97.0 AND v1.104.2):

* /user/list    -> {users, total, page, page_size, total_pages};            page_size (max 100)
* /v2/team/list -> {teams, total, page, page_size, total_pages};            page_size (max 100)
* /key/list     -> {keys, total_count, current_page, total_pages};          size (max 100)
"""

from __future__ import annotations

import pytest

from litellm_as_code.api import LiteLLMClient
from litellm_as_code.types import ReconcilerError


class _ScriptedRequest:
    """Stand-in for client._request keyed by (path, page), recording calls."""

    def __init__(self, envelopes: dict[tuple[str, int], dict], default: dict | None = None):
        self.envelopes = envelopes
        self.default = default if default is not None else {}
        self.calls: list[tuple[str, str, dict]] = []

    def __call__(self, method, path, *, json=None, params=None, retry=False):
        page = (params or {}).get("page", 1)
        self.calls.append((method, path, dict(params or {})))
        payload = self.envelopes.get((path, page), self.default)
        return payload


def _client_with(script: _ScriptedRequest) -> LiteLLMClient:
    client = LiteLLMClient("http://fake:4000", "k")
    client._request = script  # type: ignore[method-assign]
    return client


def _user(i: int) -> dict:
    return {"user_id": f"u-{i}", "user_email": f"u{i}@x.dev"}


def test_users_spread_over_2_pages_fully_collected():
    page1 = {
        "users": [_user(i) for i in range(1, 101)],
        "total": 102,
        "page": 1,
        "page_size": 100,
        "total_pages": 2,
    }
    page2 = {
        "users": [_user(i) for i in range(101, 103)],
        "total": 102,
        "page": 2,
        "page_size": 100,
        "total_pages": 2,
    }
    script = _ScriptedRequest({("/user/list", 1): page1, ("/user/list", 2): page2})
    users = _client_with(script).list_users()

    assert [u["user_id"] for u in users] == [
        f"u-{i}" for i in range(1, 103)
    ]
    assert script.calls == [
        ("GET", "/user/list", {"page": 1, "page_size": 100}),
        ("GET", "/user/list", {"page": 2, "page_size": 100}),
    ]


def test_keys_spread_over_pages_fully_collected_with_size_param():
    def envelope(p: int, n: int):
        return {
            "keys": [{"token": f"tok-{p}-{i}"} for i in range(n)],
            "total_count": 250,
            "current_page": p,
            "total_pages": 3,
        }

    script = _ScriptedRequest(
        {
            ("/key/list", 1): envelope(1, 100),
            ("/key/list", 2): envelope(2, 100),
            ("/key/list", 3): envelope(3, 50),
        }
    )
    keys = _client_with(script).list_keys()

    assert len(keys) == 250
    size100 = {"return_full_object": "true", "page": 1, "size": 100}
    assert script.calls[0] == ("GET", "/key/list", size100)
    assert all(call[2].get("size") == 100 for call in script.calls)
    assert all("page_size" not in call[2] for call in script.calls)
    assert all(call[2].get("return_full_object") == "true" for call in script.calls)
    assert [call[2]["page"] for call in script.calls] == [1, 2, 3]


def test_total_shortfall_raises_reconciler_error():
    envelope = {
        "users": [_user(i) for i in range(1, 4)],
        "total": 27,
        "page": 1,
        "page_size": 100,
        "total_pages": 1,
    }
    script = _ScriptedRequest({("/user/list", 1): envelope})

    with pytest.raises(ReconcilerError) as excinfo:
        _client_with(script).list_users()
    assert "3 of 27" in str(excinfo.value)
    assert "/user/list" in str(excinfo.value)
    assert len(script.calls) == 1


def test_legacy_envelope_without_pagination_keys_returns_single_page():
    envelope = {"users": [_user(1), _user(2)]}
    script = _ScriptedRequest({("/user/list", 1): envelope})

    users = _client_with(script).list_users()

    assert [u["user_id"] for u in users] == ["u-1", "u-2"]
    assert len(script.calls) == 1


def test_empty_live_state_returns_empty_list():
    envelope = {"users": [], "total": 0, "page": 1, "page_size": 100, "total_pages": 0}
    script = _ScriptedRequest({("/user/list", 1): envelope})

    assert _client_with(script).list_users() == []
    assert len(script.calls) == 1


def test_teams_spread_over_2_pages_fully_collected():
    def team(i: int):
        return {"team_id": f"t-{i}", "team_alias": f"team-{i}"}

    page1 = {
        "teams": [team(i) for i in range(1, 101)],
        "total": 105,
        "page": 1,
        "page_size": 100,
        "total_pages": 2,
    }
    page2 = {
        "teams": [team(i) for i in range(101, 106)],
        "total": 105,
        "page": 2,
        "page_size": 100,
        "total_pages": 2,
    }
    script = _ScriptedRequest({("/v2/team/list", 1): page1, ("/v2/team/list", 2): page2})

    teams = _client_with(script).list_teams()

    assert [t["team_id"] for t in teams] == [f"t-{i}" for i in range(1, 106)]
    assert [call[2]["page"] for call in script.calls] == [1, 2]
    assert all(call[2]["page_size"] == 100 for call in script.calls)
