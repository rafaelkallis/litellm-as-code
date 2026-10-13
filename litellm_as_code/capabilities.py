"""Run-resolved client capabilities — the single home for compat knowledge.

Two compat strategies coexisted before this module (issue #27); both remain
valid *tools*, but they are single-homed here now:

- **behavior probing** — the ``/organization`` router's enterprise gate is
  detected from the 403 error shape (``ORG_GATE_MARKERS``) on the first org
  call (classification moved here from ``resources/organizations.py``);
- **version thresholding** — model-cost persistence was decided by a padded
  ``proxy_version()`` tuple comparison against ``(1, 102, 0)`` (moved here
  from ``resources/models.py::_costs_persist``).

``capabilities(client)`` resolves ONE ``Capabilities`` per client (one
client == one run, ``cli.py``), memoized on the client like its own version
probe. ``resources/`` and ``exporter.py`` consume only ``Capabilities``:
**no ``proxy_version()`` tuple comparisons may exist outside this module**
(a grep-pinned test in tests/test_capabilities.py proves it).

Resolution order per capability (AGENTS.md §4):

1. **version hint** — cheap, orderable; it only *orders probes*, it never
   alone decides behavior (upstream reverts and backports happen — a
   threshold constant is correct only for the probed generations). The
   underlying OpenAPI probe is at most ONE HTTP ``GET /openapi.json`` per
   run (cached on the client); hint *derivation* from it is pure.
2. **behavior probe** — the real call's outcome upgrades unknowns. The org
   gate is remembered for the rest of the run. No version hint is consulted
   for orgs: a LICENSED >= 1.102 proxy exposes /organization, so a version
   tuple cannot decide this — and a tolerated 403 must never be pre-covered
   by a hint ("contradicted" is exactly what probing is for).
3. **degrade** — an unknown capability degrades with a warning; the run
   hard-fails only when the spec itself requires the capability
   (organizations declared on a gated proxy).

``None`` states and their one documented fallback each:

- ``model_costs_persist is None`` (OpenAPI probe unavailable/unparseable):
  apply runs verify per-model whether a cost-only PATCH stuck (accept what
  did not); ``--dry-run`` writes nothing so cannot verify and keeps showing
  the cost update. That fallback's mechanics live exactly once, in the
  models reconciler's acceptance phase (it needs the model-cost compare
  helpers there).
- ``org_endpoints_available``: ``None`` until the first
  ``/organization/list`` call — that call IS the probe (no extra HTTP
  round-trip is ever spent, so export traffic is unchanged); a gate-shaped
  403 upgrades ``None -> False``, a successful listing ``None -> True``.

Resolution is **lazy**: ``capabilities(client)`` costs no HTTP at all until
a capability value is actually read (``model_costs_persist`` pays the
cached OpenAPI probe; the org capability never touches it).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from .types import ReconcilerError

if TYPE_CHECKING:
    from .api import LiteLLMClient

# -- capability facts (each constant lives here and nowhere else) -----------

# Model-cost persistence cutoff — the version HINT (live-verified 2026-10:
# v1.97.0 persists per-deployment costs, v1.104.2 does not; upstream
# 76cb0fec1 removed the persistence in 1.102.0). A hint only: upstream could
# revert or move the boundary; when the probe is unavailable the models
# reconciler falls back to apply-then-verify behavior (see module docstring).
_COSTS_PERSIST_BELOW = (1, 102, 0)

# The /organization router enterprise gate (>= 1.102, unlicensed proxies):
# every /organization/* call answers HTTP 403 with a body like "Organizations
# are only available for LiteLLM Enterprise users", which
# LiteLLMClient._request embeds verbatim in the ReconcilerError text.
# Tolerance requires EVERY marker below (case-insensitive) — a plain 401, a
# 500, or a 403 with a different body must never be swallowed as the gate
# (see tests/test_org_gate.py).
ORG_GATE_MARKERS = ("403", "enterprise")


def costs_persist_hint(version: tuple[int, ...] | None) -> bool | None:
    """Version-hint resolution for ``model_costs_persist``.

    True below the cutoff (costs persist), False at/above it (costs are
    server-derived), None when the probe is unavailable or unparseable.

    The cutoff compare pads RIGHT so a truncated probe ("1.102" ->
    ``(1, 102)``) is not element-wise compared as OLDER than 1.102 (the
    shortest tuple would win that comparison).
    """
    if not isinstance(version, tuple):
        return None
    return version + (0,) * (3 - len(version)) < _COSTS_PERSIST_BELOW


def is_org_gate_error(exc: BaseException) -> bool:
    """True only when the error text carries every enterprise-gate marker."""
    text = str(exc).lower()
    return all(marker in text for marker in ORG_GATE_MARKERS)


class Capabilities:
    """One run's knowledge of what the proxy supports (issue #27).

    Memoized once per client via ``capabilities()``; consumers treat ``None``
    as unknown and follow the documented fallbacks (module docstring).

    Version-derived facts are derived purely from the client's
    ``proxy_version()`` probe on each read — the probe itself is cached by
    the client (at most one HTTP GET per run), so nothing is duplicated
    here and a re-initialized probe (tests, exotic proxies) is honored.
    The org gate is the one piece of knowledge this run *learns* lazily —
    a first-call 403 upgrades it client-side, mirroring the client's own
    version-probe cache semantics.
    """

    def __init__(self, client: LiteLLMClient) -> None:
        self._client = client
        self._org_gate: bool | None = None  # upgraded by the first org call

    # -- version-derived facts ---------------------------------------------
    @property
    def probe_available(self) -> bool:
        """True when the OpenAPI probe served a parseable version.

        False on deployments that disable the OpenAPI docs (or serve an
        unparseable version): every version hint is then unknown and the
        behavior fallbacks engage. This is a probe STATE, never a gate —
        an unknown probe must not disable behavior probing.
        """
        return isinstance(self._client.proxy_version(), tuple)

    @property
    def model_costs_persist(self) -> bool | None:
        """Whether per-deployment model costs round-trip on this proxy.

        True below the cutoff (costs persist), False at/above it (costs are
        server-derived), None when the probe is unavailable — callers fall
        back to apply-then-verify behavior (one place: the models
        reconciler's acceptance phase).
        """
        return costs_persist_hint(self._client.proxy_version())

    # -- org enterprise gate (behavior-probed on the first org call) --------
    @property
    def org_endpoints_available(self) -> bool | None:
        """``None`` until probed; never guessed from a version hint.

        False = the proxy 403'd the enterprise gate (unlicensed >= 1.102);
        True = at least one /organization listing succeeded this run.
        """
        return self._org_gate

    def list_organizations(self) -> list[dict[str, Any]] | None:
        """``client.list_organizations()``, tolerating the enterprise gate.

        Single home of that tolerance — the reconciler and the exporter
        share this one implementation. Returns the live rows, or ``None``
        when the proxy 403'd the gate (upgrading the org knowledge for the
        rest of the run). Any other ``ReconcilerError`` (401, 500, a 403
        without the gate body, ...) propagates unchanged.
        """
        try:
            rows = self._client.list_organizations()
        except ReconcilerError as exc:
            if is_org_gate_error(exc):
                self._org_gate = False
                return None
            raise
        self._org_gate = True
        return rows


def capabilities(client: LiteLLMClient) -> Capabilities:
    """Resolve (at most once per client == once per run) the run's facts.

    Memoized on the client (the ``_capabilities_cache`` slot in api.py,
    sentinel-style sibling of ``_proxy_version_cache``). Costs no HTTP until
    a version-derived capability value is read — see the module docstring.
    """
    cache: Any = getattr(client, "_capabilities_cache", None)
    if cache is None:
        cache = Capabilities(client)
        client._capabilities_cache = cache
    return cast(Capabilities, cache)
