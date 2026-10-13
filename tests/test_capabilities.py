"""Run-resolved client capabilities (issue #27).

Pins the single-home contract: the model-cost version hint (right-padded
cutoff compare), the OpenAPI probe-None fallback states, per-client
memoization, the org enterprise gate as a behavior probe (upgraded lazily,
never version-guessed, probe-untouched), and the grep-provable invariant
that no resource reconciler or the exporter references the version probe.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import litellm_as_code
import pytest

from litellm_as_code.capabilities import (
    ORG_GATE_MARKERS,
    Capabilities,
    capabilities,
    is_org_gate_error,
)
from litellm_as_code.reconciler import reconcile
from litellm_as_code.types import ReconcilerError

from tests import make_fake_client

# Exact shape LiteLLMClient._request produces for the gate (same as
# tests/test_org_gate.py): "{method} {path} failed: {http error} {body}".
GATE_TEXT = (
    "GET /organization/list failed: 403 Client Error Forbidden for url: "
    "http://proxy:4000/organization/list Organizations are only available for "
    "LiteLLM Enterprise users"
)


# -- version-derived facts ---------------------------------------------------


def test_capabilities_expose_version_facts_through_the_probe():
    """The hint and the probe state come from the client's OpenAPI probe."""
    client, fake = make_fake_client()
    caps = capabilities(client)
    # default fake = the pinned v1.97.0 generation (costs persist)
    assert caps.model_costs_persist is True
    assert caps.probe_available is True

    # >= 1.102 generations: costs are server-derived, probe still served
    fake.proxy_version = (1, 104, 2)
    assert caps.model_costs_persist is False
    assert caps.probe_available is True

    # OpenAPI docs disabled: every hint unknown — fallback territory
    fake.proxy_version = None
    assert caps.model_costs_persist is None
    assert caps.probe_available is False


def test_version_facts_honor_a_reinitialized_probe():
    """Facts derive live from client.proxy_version() — which the client
    itself caches for real proxies (one HTTP GET per run), while tests re-
    initialize the knob mid-run and must be honored."""
    client, fake = make_fake_client()
    caps = capabilities(client)
    assert caps.model_costs_persist is True      # v1.97.0 hint (fake default)
    fake.proxy_version = (1, 104, 2)
    assert caps.model_costs_persist is False
    fake.proxy_version = None
    assert caps.model_costs_persist is None


# -- memoization --------------------------------------------------------------


def test_capabilities_are_memoized_per_client():
    """capabilities() resolves ONE object per client (once per run)."""
    client, _fake = make_fake_client()
    assert capabilities(client) is capabilities(client)

    other, _other_fake = make_fake_client()
    assert capabilities(other) is not capabilities(client)


def test_capabilities_object_costs_no_http_until_a_fact_is_read():
    """Probe accounting: construction and org-gate behavior probing never
    touch the OpenAPI probe. (The real client caches proxy_version() itself,
    so every version-fact read is at most ONE HTTP GET per run; this test
    pins that the org path does not pay even that one.)
    """
    client, fake = make_fake_client()
    probes: list[int] = []
    orig_probe = fake.proxy_version

    def _counting_probe():
        probes.append(1)
        return orig_probe

    client.proxy_version = _counting_probe  # type: ignore[method-assign]
    client.list_organizations = lambda: []  # type: ignore[method-assign]

    caps = capabilities(client)
    assert probes == []  # construction: no HTTP

    assert caps.list_organizations() == []
    assert caps.org_endpoints_available is True
    assert probes == []  # org path: still no OpenAPI GET

    _ = caps.model_costs_persist  # the version fact pays the (cached) probe
    assert probes == [1]


# -- org enterprise gate ------------------------------------------------------


def _gate_client() -> Any:
    client, _fake = make_fake_client()

    def _raise_gate():
        raise ReconcilerError(GATE_TEXT)

    client.list_organizations = _raise_gate  # type: ignore[method-assign]
    return client


def test_org_gate_error_requires_every_marker():
    """Case-insensitive conjunctive marker matching; non-gates never match."""
    assert ORG_GATE_MARKERS == ("403", "enterprise")
    assert is_org_gate_error(ReconcilerError(GATE_TEXT))
    assert is_org_gate_error(ReconcilerError("403 body: ENTERPRISE Users"))
    assert not is_org_gate_error(ReconcilerError("403 unauthorized"))
    assert not is_org_gate_error(ReconcilerError("500 Server Error"))


def test_org_gate_hit_upgrades_unknown_and_is_remembered():
    client = _gate_client()
    caps = capabilities(client)
    assert caps.org_endpoints_available is None  # unknown before the first org call

    assert caps.list_organizations() is None  # tolerated: gate answer

    # upgraded client-side, for the rest of the run
    assert caps.org_endpoints_available is False
    assert caps.list_organizations() is None  # tolerance is not one-shot
    assert caps.org_endpoints_available is False


def test_org_listing_success_upgrades_to_available():
    client, _fake = make_fake_client()
    client.list_organizations = lambda: [  # type: ignore[method-assign]
        {"organization_id": "org-1", "organization_alias": "acme"}
    ]
    caps = capabilities(client)
    assert caps.org_endpoints_available is None

    assert caps.list_organizations() == [
        {"organization_id": "org-1", "organization_alias": "acme"}
    ]
    assert caps.org_endpoints_available is True


def test_non_gate_error_propagates_and_never_upgrades():
    """A 5xx (or 401, or a markerless 403) is not the gate: re-raise, and the
    org knowledge stays unknown."""
    client, _fake = make_fake_client()

    def _raise_500():
        raise ReconcilerError(
            "GET /organization/list failed: 500 Server Error: Internal Server Error"
        )

    client.list_organizations = _raise_500  # type: ignore[method-assign]
    caps = capabilities(client)

    with pytest.raises(ReconcilerError):
        caps.list_organizations()
    assert caps.org_endpoints_available is None


def test_gate_degradation_stills_runs_through_reconcile(tmp_path):
    """End to end: an org-free spec on a gated proxy degrades + reconciles
    clean (the Capabilities path wired into reconcile via organizations)."""
    client = _gate_client()
    spec_path = tmp_path / "spec.yml"
    spec_path.write_text("{}")

    plan = reconcile(spec_path, client, dry_run=False)

    assert [d for d in plan.diffs if d.resource_type == "organization"] == []
    assert capabilities(client).org_endpoints_available is False


# -- single-home invariant (grep-provable acceptance, issue #27) --------------


def test_no_version_probe_references_outside_capabilities():
    """resources/ and exporter.py consume Capabilities — no version-probe
    references of their own (the threshold/hint comparison lives exactly
    once, in capabilities.py)."""
    root = Path(litellm_as_code.__file__).resolve().parent
    consumers = sorted((root / "resources").glob("*.py")) + [root / "exporter.py"]
    for path in consumers:
        src = path.read_text(encoding="utf-8")
        assert "proxy_version" not in src, (
            f"{path.name} must not talk to the version probe directly — "
            "capabilities.py is the single home (issue #27)"
        )


def test_capability_consumers_import_the_single_home():
    """The models-cost and org-gate paths (and the exporter's shared gate
    tolerance) import from litellm_as_code.capabilities."""
    root = Path(litellm_as_code.__file__).resolve().parent
    for rel in ("resources/models.py", "resources/organizations.py", "exporter.py"):
        src = (root / rel).read_text(encoding="utf-8")
        assert "from ..capabilities import capabilities" in src or (
            "from .capabilities import capabilities" in src
        ), f"{rel} must consume capabilities (issue #27)"


def test_cutoff_comparison_sits_exactly_once():
    """The (1, 102, 0) fact exists exactly once: in capabilities.py."""
    root = Path(litellm_as_code.__file__).resolve().parent
    for path in sorted((root / "resources").glob("*.py")) + [root / "exporter.py"]:
        src = path.read_text(encoding="utf-8")
        assert "(1, 102, 0)" not in src, (
            f"{path.name} hard-codes the cost cutoff — move the fact to "
            "capabilities.py (issue #27)"
        )


# -- construction robustness -----------------------------------------------


def test_probeless_deployment_still_gets_full_org_behavior():
    """A probe-less deployment still gets full org behavior: probe_available
    False must not disable behavior probing (it is a state, not a gate)."""
    client, fake = make_fake_client()
    fake.proxy_version = None
    client.list_organizations = lambda: [{"organization_id": "org-1"}]  # type: ignore[method-assign]
    caps = capabilities(client)
    assert caps.probe_available is False
    assert caps.org_endpoints_available is None
    assert caps.list_organizations() == [{"organization_id": "org-1"}]
    assert caps.org_endpoints_available is True


def test_capabilities_build_on_any_client_shaped_object():
    """Capabilities(...) builds on any client-shaped object (no init-time
    HTTP, optional cache slot) — the _VersionStub pattern in
    tests/test_model_costs.py relies on this."""
    class _Stub:
        def __init__(self):
            self.calls = 0

        def proxy_version(self):  # never called here
            self.calls += 1
            return (1, 97, 0)

    stub = _Stub()
    caps = Capabilities(stub)
    assert caps.org_endpoints_available is None
    assert stub.calls == 0
    assert capabilities(stub) is capabilities(stub)
