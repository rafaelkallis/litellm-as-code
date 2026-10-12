"""LiteLLMClient.proxy_version capability-probe tests.

GET /openapi.json is a read-only probe whose `info.version` field carries the
proxy's own version (live-verified: v1.97.0 and v1.104.2 both serve it —
e.g. "1.104.2" -> (1, 104, 2)). Models reconciliation uses it to key
version-specific behavior (per-deployment cost persistence); deployments may
disable the OpenAPI docs, so a failed/unparseable probe must degrade to None
("unknown") — never crash the run.
"""

from __future__ import annotations

from litellm_as_code.api import LiteLLMClient
from litellm_as_code.types import ReconcilerError


def _client() -> LiteLLMClient:
    return LiteLLMClient("http://fake:4000", "test-admin-key")


def test_proxy_version_parses_openapi_info_version():
    client = _client()
    calls = []

    def scripted(method: str, path: str, **_: object) -> dict:
        calls.append((method, path))
        return {"info": {"version": "1.104.2"}}

    client._request = scripted  # type: ignore[method-assign]
    assert client.proxy_version() == (1, 104, 2)
    assert calls == [("GET", "/openapi.json")]
    # cached: a second call must not re-issue the request
    assert client.proxy_version() == (1, 104, 2)
    assert calls == [("GET", "/openapi.json")]


def test_proxy_version_tolerates_v_prefix_and_prerelease_suffix():
    client = _client()
    client._request = lambda *a, **k: {"info": {"version": "v1.102.0"}}  # type: ignore[method-assign]
    assert client.proxy_version() == (1, 102, 0)


def test_proxy_version_unavailable_returns_none_and_stays_cached():
    client = _client()
    calls = []

    def failing(*_a: object, **_k: object) -> dict:
        calls.append(1)
        raise ReconcilerError("GET /openapi.json failed: 404")

    client._request = failing  # type: ignore[method-assign]
    assert client.proxy_version() is None
    assert client.proxy_version() is None  # cached: no second probe
    assert len(calls) == 1


def test_proxy_version_garbage_version_returns_none():
    client = _client()
    client._request = lambda *a, **k: {"info": {"version": "n/a"}}  # type: ignore[method-assign]
    assert client.proxy_version() is None
