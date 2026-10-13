#!/usr/bin/env python3
"""OpenAPI contract digest of the endpoints litellm-as-code consumes (issue #26).

The reconciler depends on a small, known slice of the LiteLLM admin API: the
``(method, path)`` pairs called through ``litellm_as_code/api.py``.  Upstream
ships 4-5 stable releases a month; booting a proxy per release to discover a
contract change is impractical (see the compat-matrix harness).  Instead we
capture a *normalized digest of just the consumed operations* from the served
``GET /openapi.json`` — a schema change on our surface becomes visible in a
per-endpoint diff of a few kilobytes, days before a behavioral break gets hit
by a nightly leg.

Design decisions
----------------
**Consumed surface only.**  LiteLLM serves 500+ paths (provider routers,
model lists, enterprise bits) that churn daily with zero impact on us; a
whole-spec diff would cry wolf.  ``CONSUMED_ENDPOINTS`` is the contract —
``endpoints_from_client_source`` parses it back out of ``api.py``'s
``self._request(...)`` / ``self._paginated_list(...)`` call sites, and the
mock test suite fails loudly on any drift between the two.

``GET /openapi.json`` itself is consumed for version probing but excluded
from the digest (``DIGEST_EXCLUDED``): its response schema is the *entire
spec*, inlining it would be circular noise.

**Normalization makes diffs meaningful.**  For every consumed operation we
keep parameters (name/in/required/schema), the requestBody (required + content
schemas) and responses (status → schema), with ``$ref`` chains resolved
inline against ``components`` and prose keys pruned.  The result is
deep-sorted and hashed **per endpoint**, so:

- identical shapes → byte-identical file (no diff churn, determinism AC);
- a rename/retypo in a consumed param is a tiny, human-readable delta;
- enterprise-gated routers that a stack does not serve are recorded as
  ``{"absent": true}`` rather than crashing the capture (their
  reappearing/disappearing is itself visible in the digest diff).

**Where it runs.**  ``scripts/compat_matrix.py --digest-out`` captures one
digest per matrix leg (the proxy is already healthy after pytest).  The
nightly canary workflow (issue #25) commits the last-good digest **only on
all-green runs**; ``manage --digest-diff TEXT`` renders contract drift onto
the ``compat-canary`` tracking issue.  Drift alone never opens a tracker —
it is a watch signal that surfaces *before* a behavioral break.

CLI::

    openapi_digest capture --base-url http://localhost:4001 --api-key KEY \
        --out digest.json
    openapi_digest diff --old last-known-good.json --new digest.json

Exit codes: 0 = ok/identical, 1 = error, 2 = differs (repo CLI convention).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.error
import urllib.request
from typing import Any

HARNESS = "openapi_digest"
DIGEST_FORMAT = 1  # bump when the normalization shape changes (forces one diff)

# Every (method, path-template) pair litellm_as_code/api.py talks to. The
# placeholders come from the client call-site variables ({name}, {model_id},
# {guardrail_id}, {policy_id}); spec-side parameter names may differ
# (e.g. the spec calls it /credentials/{credential_name}) — resolution is by
# shape, see spec_path_for(). GET /openapi.json is consumed for proxy_version
# probing but excluded from the digest (its schema IS the spec).
CONSUMED_ENDPOINTS: frozenset[tuple[str, str]] = frozenset(
    {
        ("DELETE", "/credentials/{name}"),
        ("DELETE", "/guardrails/{guardrail_id}"),
        ("DELETE", "/organization/delete"),
        ("DELETE", "/organization/member_delete"),
        ("DELETE", "/policies/{policy_id}"),
        ("GET", "/budget/list"),
        ("GET", "/credentials"),
        ("GET", "/key/list"),
        ("GET", "/model/info"),
        ("GET", "/openapi.json"),
        ("GET", "/organization/list"),
        ("GET", "/policies/list"),
        ("GET", "/team/info"),
        ("GET", "/user/list"),
        ("GET", "/v2/guardrails/list"),
        ("GET", "/v2/team/list"),
        ("PATCH", "/credentials/{name}"),
        ("PATCH", "/guardrails/{guardrail_id}"),
        ("PATCH", "/model/{model_id}/update"),
        ("PATCH", "/organization/member_update"),
        ("PATCH", "/organization/update"),
        ("POST", "/budget/delete"),
        ("POST", "/budget/new"),
        ("POST", "/budget/update"),
        ("POST", "/credentials"),
        ("POST", "/guardrails"),
        ("POST", "/key/delete"),
        ("POST", "/key/generate"),
        ("POST", "/key/update"),
        ("POST", "/model/delete"),
        ("POST", "/model/new"),
        ("POST", "/organization/member_add"),
        ("POST", "/organization/new"),
        ("POST", "/policies"),
        ("POST", "/team/delete"),
        ("POST", "/team/member_add"),
        ("POST", "/team/member_delete"),
        ("POST", "/team/member_update"),
        ("POST", "/team/new"),
        ("POST", "/team/update"),
        ("POST", "/user/delete"),
        ("POST", "/user/new"),
        ("POST", "/user/update"),
        ("PUT", "/policies/{policy_id}"),
    }
)

DIGEST_EXCLUDED: frozenset[tuple[str, str]] = frozenset({("GET", "/openapi.json")})

# Prose/churn keys pruned anywhere in a normalized subtree.
_PRUNED_KEYS = frozenset(
    {
        "description",
        "title",
        "example",
        "examples",
        "default",
        "externalDocs",
        "tags",
        "summary",
        "operationId",
        "servers",
        "security",
        "deprecated",  # regressed to churn: upstream flips it on stale ops
    }
)
_MAX_REF_DEPTH = 8


# ---------------------------------------------------------------- extraction


def endpoints_from_client_source(source: str) -> set[tuple[str, str]]:
    """Pull the (method, path-template) pairs out of ``api.py`` call sites.

    Covers the two talk patterns in the client:

    - ``self._request("METHOD", <path>, ...)`` — ``<path>`` is a string
      literal, an f-string (whose ``quote(var, safe='')`` placeholder becomes
      ``{var}``), or the ``path`` parameter inside ``_paginated_list``;
    - ``self._paginated_list("/path", ...)`` — every paginated list is a GET.

    Unknown argument shapes raise (a new talking pattern must be taught here
    rather than silently dropped from the contract).
    """
    endpoints: set[tuple[str, str]] = set()

    def _normalize_literal(raw: str) -> str:
        # f"/credentials/{quote(name, safe='')}" -> "/credentials/{name}"
        return re.sub(
            r"\{quote\((\w+),\s*safe=_{0,2}'{2}\)\}",
            r"{\1}",
            raw,
        )

    call = re.compile(r"self\.(?:_request|_paginated_list)\(\s*")
    for match in call.finditer(source):
        tail = source[match.end() :]
        args = _split_first_two_args(tail)
        if args is None:
            raise ValueError(
                "call site without two positional arguments (method, path) "
                f"near {match.group(0)!r} — teach endpoints_from_client_source"
            )
        argv0, argv1 = args
        if "self._paginated_list" in match.group(0):
            endpoints.add(("GET", _normalize_literal(_unquote(argv0))))
            continue
        method = argv0  # first arg of _request is always "METHOD"
        if method not in ('"GET"', '"POST"', '"PATCH"', '"PUT"', '"DELETE"'):
            raise ValueError(f"unexpected _request method argument: {method!r}")
        path = _unquote(argv1)
        if path is None:
            if argv1 == "path":
                continue  # inside _paginated_list; its literal is handled above
            raise ValueError(f"unexpected _request path argument: {argv1!r}")
        endpoints.add((method.strip('"'), _normalize_literal(path)))
    return endpoints


def _unquote(token: str) -> str | None:
    """Strip one optional f prefix and the surrounding double quotes.

    Returns the raw body (e.g. ``/credentials/{name}`` from
    ``f"/credentials/{quote(name, safe='')}"``), or None if the token is not
    a string literal (e.g. the bare ``path`` identifier inside
    ``_paginated_list``).
    """
    body = token[1:] if token.startswith("f") else token
    if body.startswith('"') and body.endswith('"') and len(body) >= 2:
        return body[1:-1]
    return None


def _split_first_two_args(tail: str) -> tuple[str, str] | None:
    """Cut the first two comma-separated argument tokens out of a call.

    Returns the (arg0, arg1) string literals (with surrounding quotes kept so
    the caller can tell plain vs f-string), or None if fewer than two args
    appear before the end of the snippet window (shouldn't happen on real
    call sites — the window is the rest of the file).
    """
    tokens: list[str] = []
    current: list[str] = []
    depth = 0
    in_string: str | None = None
    for char in tail:
        if in_string is not None:
            current.append(char)
            if char == in_string:
                # a doubled quote inside a string would need un-escaping; the
                # client's literals never contain one
                in_string = None
            continue
        if char == '"':
            in_string = char
            current.append(char)
            continue
        if char in "([{":
            depth += 1
            current.append(char)
            continue
        if char in ")]}":
            if depth == 0 and char == ")":
                break  # call ends
            depth -= 1
            current.append(char)
            continue
        if char == "," and depth == 0:
            tokens.append("".join(current).strip())
            current = []
            if len(tokens) == 2:
                return (tokens[0], tokens[1])
            continue
        current.append(char)
    tokens.append("".join(current).strip())
    if len(tokens) >= 2:
        return (tokens[0], tokens[1])
    return None


# ---------------------------------------------------------------- normalization


def _resolve_ref(ref: str, components: dict[str, Any], depth: int) -> Any:
    """Resolve ``#/components/...`` against the spec's components section.

    Depth-capped: an upstream self-referential or absurdly nested schema
    bottoms out as ``{"$ref-depth-cutoff": ref}`` instead of exploding the
    capture.  Anything unresolvable becomes ``{"$ref-unresolved": ref}``.
    """
    if depth > _MAX_REF_DEPTH:
        return {"$ref-depth-cutoff": ref}
    node: Any = components
    parts = ref.lstrip("#/").split("/")
    # components is seeded as the root; the "#/components" prefix is consumed
    # here, so only the sections beneath it are walked
    if parts and parts[0] == "components":
        parts = parts[1:]
    try:
        for part in parts:
            if not isinstance(node, dict) or part not in node:
                raise KeyError(part)
            node = node[part]
    except KeyError:
        return {"$ref-unresolved": ref}
    return _prune_node(node, components, depth + 1)


def _prune_node(node: Any, components: dict[str, Any], depth: int) -> Any:
    """Recursively normalize: resolve refs, drop prose, keep contract keys."""
    if depth > _MAX_REF_DEPTH:
        return {"$ref-depth-cutoff": "#too-deep"}
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/"):
            return _resolve_ref(ref, components, depth)
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key.startswith("x-") or key in _PRUNED_KEYS:
                continue
            if key == "$ref":
                # non-#/... refs (URLs) are opaque; keep them verbatim
                out[key] = value
                continue
            if key == "parameters":
                out[key] = [_prune_node(item, components, depth + 1) for item in value]
            elif key == "responses":
                out[key] = {
                    str(status): _prune_response(resp, components, depth)
                    for status, resp in value.items()
                }
            elif key == "requestBody":
                out[key] = _prune_request_body(value, components, depth)
            else:
                out[key] = _prune_node(value, components, depth + 1)
        return out
    if isinstance(node, list):
        return [_prune_node(item, components, depth + 1) for item in node]
    return node


def _prune_request_body(body: dict[str, Any], components: dict[str, Any], depth: int) -> Any:
    """Keep requestBody's required flag + content schemas only."""
    out: dict[str, Any] = {}
    if "required" in body:
        out["required"] = body["required"]
    content = body.get("content")
    if isinstance(content, dict):
        out["content"] = {
            media_type: _prune_node(section, components, depth + 1)
            for media_type, section in content.items()
        }
    return out


def _prune_response(response: dict[str, Any], components: dict[str, Any], depth: int) -> Any:
    """Response *descriptions* are prose; keep only the content schemas."""
    content = response.get("content")
    if not isinstance(content, dict):
        return {}
    return {
        "content": {
            media_type: _prune_node(section, components, depth + 1)
            for media_type, section in content.items()
        }
    }


def _segments(path: str) -> list[str]:
    """Split a path/template into non-empty segments."""
    return [segment for segment in path.split("/") if segment]


def _is_param(segment: str) -> bool:
    return segment.startswith("{") and segment.endswith("}") and len(segment) > 2


def spec_path_for(template: str, paths: dict[str, Any]) -> str | None:
    """Map a client path template onto the served spec's concrete path.

    Matching is SEGMENT-WISE and strict: every template placeholder must land
    on a spec parameter segment (``{...}``), and every template literal
    segment must be equal.  This is what keeps ``/guardrails/{guardrail_id}``
    from matching literal siblings like ``/guardrails/list`` or
    ``/credentials/{name}`` from matching ``/credentials/migrate-encryption``.
    Spec parameter NAMES may differ (the client says ``{name}``, the spec
    says ``{credential_name}``) — that is the point.

    None on no match or an ambiguous match (the contract must be exact).
    """
    template_segments = _segments(template)
    matches: list[str] = []
    for spec_path in paths:
        spec_segments = _segments(spec_path)
        if len(spec_segments) != len(template_segments):
            continue
        ok = True
        for spec_segment, template_segment in zip(spec_segments, template_segments):
            if _is_param(template_segment):
                if not _is_param(spec_segment):
                    ok = False
                    break
            elif spec_segment != template_segment:
                ok = False
                break
        if ok:
            matches.append(spec_path)
    if len(matches) == 1:
        return matches[0]
    return None


# ---------------------------------------------------------------- digest


def _endpoint_key(method: str, template: str) -> str:
    return f"{method} {template}"


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


_ABSENT_SENTINEL = json.dumps({"absent": True}, sort_keys=True, separators=(",", ":"))


def digest_payload(spec: dict[str, Any]) -> dict[str, Any]:
    """Build the normalized consumed-surface digest from a served OpenAPI spec.

    Deterministic: same shapes → same payload → byte-identical file
    (json.dumps(sort_keys=True) in write_json).  Each endpoint carries its
    own sha256 so drift is per-endpoint, not per-blob.
    """
    components = spec.get("components", {}) if isinstance(spec.get("components"), dict) else {}
    paths = spec.get("paths", {}) if isinstance(spec.get("paths"), dict) else {}
    endpoints: dict[str, Any] = {}
    for method, template in sorted(CONSUMED_ENDPOINTS - DIGEST_EXCLUDED):
        key = _endpoint_key(method, template)
        entry: dict[str, Any]
        spec_path = spec_path_for(template, paths)
        operation = (paths.get(spec_path) or {}).get(method.lower()) if spec_path else None
        if spec_path is None or not isinstance(operation, dict):
            entry = json.loads(_ABSENT_SENTINEL)
        else:
            operation_node = _prune_node(operation, components, 0)
            canonical = json.dumps(
                {k: v for k, v in operation_node.items() if k != "parameters"}
                | {"parameters": operation_node.get("parameters", [])},
                sort_keys=True,
                separators=(",", ":"),
            )
            entry = {"operation": operation_node, "sha256": _hash(canonical)}
        endpoints[key] = entry
    return {
        "format": DIGEST_FORMAT,
        "harness": HARNESS,
        "spec_version": spec.get("info", {}).get("version"),
        "endpoints": endpoints,
    }


def digest_json(payload: dict[str, Any]) -> str:
    """Canonical file bytes for a digest payload (deterministic)."""
    return json.dumps(payload, sort_keys=True, indent=2) + "\n"


def write_digest(path: Any, payload: dict[str, Any]) -> None:
    import pathlib

    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(digest_json(payload), encoding="utf-8")


# ---------------------------------------------------------------- diff


def deep_changes(old: Any, new: Any, path: str = "") -> list[str]:
    """Human-readable list of leaf-level differences between two subtrees.

    Paths are dotted (``parameters.0.schema.type``); scalar leaves render as
    ``<json old> -> <json new>`` truncated for readability.  Order matters
    for output stability: dict keys sorted, list items compared by index.
    """
    if type(old) is not type(new):
        return [f"{path}: {_preview(old)} -> {_preview(new)}"]
    if isinstance(old, dict):
        changed: list[str] = []
        for key in sorted(set(old) | set(new)):
            here = f"{path}.{key}" if path else key
            if key not in old:
                changed.append(f"{here}: + {_preview(new[key])}")
            elif key not in new:
                changed.append(f"{here}: - {_preview(old[key])}")
            else:
                changed.extend(deep_changes(old[key], new[key], here))
        return changed
    if isinstance(old, list):
        changed = []
        for index in range(max(len(old), len(new))):
            here = f"{path}.{index}" if path else str(index)
            if index >= len(old):
                changed.append(f"{here}: + {_preview(new[index])}")
            elif index >= len(new):
                changed.append(f"{here}: - {_preview(old[index])}")
            else:
                changed.extend(deep_changes(old[index], new[index], here))
        return changed
    if old != new:
        return [f"{path}: {_preview(old)} -> {_preview(new)}"]
    return []


def _preview(value: Any, limit: int = 160) -> str:
    text = json.dumps(value, sort_keys=True, default=repr)
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


def diff_digests(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Per-endpoint contract diff between two digest payloads.

    Returns a structured report (deterministic; safe to embed in JSON
    reports):

        {"added": [...], "removed": [...], "absence_changed": [...],
         "changed": {endpoint: [leaf lines]}, "identical": bool,
         "old_version": ..., "new_version": ...}
    """
    old_eps = old.get("endpoints", {}) if isinstance(old, dict) else {}
    new_eps = new.get("endpoints", {}) if isinstance(new, dict) else {}
    added = sorted(set(new_eps) - set(old_eps))
    removed = sorted(set(old_eps) - set(new_eps))
    changed: dict[str, list[str]] = {}
    absence_changed: list[str] = []
    for endpoint in sorted(set(old_eps) & set(new_eps)):
        before, after = old_eps[endpoint], new_eps[endpoint]
        if before.get("absent") != after.get("absent"):
            absence_changed.append(
                f"{endpoint}: served -> {'absent' if after.get('absent') else 'served'}"
            )
            continue
        if before == after:
            continue
        changed[endpoint] = deep_changes(
            before.get("operation", {}), after.get("operation", {})
        )
    return {
        "identical": not (added or removed or changed or absence_changed),
        "absence_changed": absence_changed,
        "added": added,
        "changed": changed,
        "old_version": old.get("spec_version") if isinstance(old, dict) else None,
        "new_version": new.get("spec_version") if isinstance(new, dict) else None,
        "removed": removed,
    }


def render_diff(report: dict[str, Any]) -> str:
    """Render a diff_digests report as the human-readable tracking text."""
    lines: list[str] = []
    if report.get("identical"):
        return ""
    old_version = report.get("old_version")
    new_version = report.get("new_version")
    header = f"OpenAPI contract drift ({old_version or '?'} -> {new_version or '?'})"
    lines.append(header)
    if report.get("added"):
        lines.append("added endpoints:")
        lines.extend(f"  + {endpoint}" for endpoint in report["added"])
    if report.get("removed"):
        lines.append("removed endpoints:")
        lines.extend(f"  - {endpoint}" for endpoint in report["removed"])
    if report.get("absence_changed"):
        lines.append("absence changes (endpoint served/un-served by the proxy):")
        lines.extend(f"  ~ {line}" for line in report["absence_changed"])
    for endpoint, changes in sorted((report.get("changed") or {}).items()):
        lines.append(f"{endpoint}:")
        lines.extend(f"    {line}" for line in changes[:20])
        if len(changes) > 20:
            lines.append(f"    … and {len(changes) - 20} more")
    return "\n".join(lines)


# ---------------------------------------------------------------- fetch + CLI


def fetch_spec(base_url: str, api_key: str, timeout: float = 30.0) -> dict[str, Any]:
    """GET /openapi.json with the proxy's master key (Bearer)."""
    url = base_url.rstrip("/") + "/openapi.json"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (trusted harness target)
        return json.loads(response.read().decode("utf-8"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openapi_digest",
        description=(
            "Capture and diff a normalized OpenAPI digest of the endpoints "
            "litellm-as-code consumes (contract-evolution watch, issue #26)."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)

    capture = commands.add_parser(
        "capture", help="fetch /openapi.json from a live proxy and write the digest"
    )
    capture.add_argument("--base-url", required=True)
    capture.add_argument("--api-key", required=True)
    capture.add_argument("--out", type=str, default=None, help="write the digest JSON here")
    capture.add_argument(
        "--summary-only",
        action="store_true",
        help="print a one-line summary; do not write (used for probes)",
    )

    diff = commands.add_parser("diff", help="diff two digest files")
    diff.add_argument("--old", required=True)
    diff.add_argument("--new", required=True)
    diff.add_argument(
        "--text-out",
        type=str,
        default=None,
        help="also write the rendered human-readable drift text here",
    )
    return parser


def _read_json(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return payload


def _check_digest(payload: dict[str, Any], origin: str) -> None:
    if payload.get("harness") != HARNESS:
        raise ValueError(f"{origin}: not an {HARNESS} digest (harness={payload.get('harness')!r})")
    if not isinstance(payload.get("endpoints"), dict):
        raise ValueError(f"{origin}: digest has no endpoints map")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "capture":
            spec = fetch_spec(args.base_url, args.api_key)
            payload = digest_payload(spec)
            absent = sorted(
                key for key, entry in payload["endpoints"].items() if entry.get("absent")
            )
            if not args.summary_only and args.out:
                write_digest(args.out, payload)
            if args.out and not args.summary_only:
                print(f"captured {len(payload['endpoints'])} endpoints -> {args.out}")
            else:
                print(
                    f"captured {len(payload['endpoints'])} endpoints "
                    f"(spec_version={payload['spec_version']}, absent={len(absent)})"
                )
            if absent:
                print("\n".join(f"  absent: {key}" for key in absent))
            return 0
        if args.command == "diff":
            old = _read_json(args.old)
            new = _read_json(args.new)
            _check_digest(old, args.old)
            _check_digest(new, args.new)
            report = diff_digests(old, new)
            text = render_diff(report)
            if text:
                print(text)
                if args.text_out:
                    with open(args.text_out, "w", encoding="utf-8") as handle:
                        handle.write(text + "\n")
                return 2
            print("digests identical")
            return 0
        parser.error(f"unknown command {args.command!r}")
        return 1
    except (ValueError, OSError, json.JSONDecodeError, urllib.error.URLError) as exc:
        print(f"openapi_digest: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:  # pragma: no cover - operator interrupt
        return 1


if __name__ == "__main__":
    sys.exit(main())
