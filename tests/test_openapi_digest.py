"""Mock tests for the OpenAPI contract digest (scripts/openapi_digest.py).

No proxy is booted here: the capture CLI is exercised through a stubbed
``fetch_spec`` and every normalization case runs against a small synthetic
spec that mirrors the shapes of the five-yours-a-year upstream spec (refs,
prose keys, self-references, enterprise-gated routers, literal path
siblings). The end-to-end contract stays pinned —
``test_client_source_matches_consumed_endpoints`` reads the REAL
``litellm_as_code/api.py`` and fails on any drift between the client's call
sites and ``CONSUMED_ENDPOINTS`` (acceptance criterion: adding an endpoint
without a digest entry fails a test).
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
_REPO = _SCRIPTS.parent


def _load(name: str):
    path = _SCRIPTS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader  # file-path import: loader is always present
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # MANDATORY before exec_module (dataclass bug)
    spec.loader.exec_module(module)
    return module


digest = _load("openapi_digest")


# ------------------------------------------------------------- spec fixture


def _spec() -> dict:
    """Synthetic proxy spec exercising the normalization surface."""
    return {
        "openapi": "3.1.0",
        "info": {"version": "1.99.0", "title": "LiteLLM (prose)"},
        "paths": {
            "/user/new": {
                "post": {
                    "operationId": "newUser",
                    "description": "prose to prune",
                    "tags": ["user"],
                    "x-internal": True,
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/NewUserRequest"}
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "prose response",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "token": {"type": "string"},
                                            "note": {
                                                "type": "string",
                                                "description": "prune me",
                                                "example": "and me",
                                            },
                                        },
                                    }
                                }
                            },
                        }
                    },
                }
            },
            "/credentials/{credential_name}": {
                "patch": {
                    "parameters": [
                        {
                            "name": "credential_name",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                            "description": "prose in parameter",
                        }
                    ],
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "credential_values": {
                                            "type": "object",
                                            "x-vendor": "churn",
                                        }
                                    },
                                }
                            }
                        }
                    },
                    "responses": {},
                }
            },
            "/guardrails/list": {"get": {"responses": {}}},  # literal sibling trap
            "/guardrails/{guardrail_id}": {
                "delete": {"responses": {"200": {"content": {"application/json": {"schema": {"type": "object"}}}}}}
            },
        },
        "components": {
            "schemas": {
                "NewUserRequest": {
                    "type": "object",
                    "required": ["user_id"],
                    "properties": {
                        "user_id": {"type": "string", "description": "prose"},
                        "user_email": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                        "nested": {"$ref": "#/components/schemas/NewUserRequest"},
                        "missing": {"$ref": "#/components/schemas/NotThere"},
                        "unpong": {"$ref": "#/components/schemas/NewUserRequest"},
                    },
                }
            }
        },
    }


def _mini(spec: dict, endpoints: set[tuple[str, str]], monkeypatch) -> dict:
    """digest_payload against a pinned subset of the contract."""
    monkeypatch.setattr(digest, "CONSUMED_ENDPOINTS", frozenset(endpoints))
    return digest.digest_payload(spec)


# ---------------------------------------------------------------- extraction


def test_client_source_matches_consumed_endpoints():
    """The REAL client call sites and CONSUMED_ENDPOINTS must be identical.

    Drift in either direction fails: a new endpoint without a digest entry,
    or a stale entry for an endpoint the client no longer calls.
    """
    source = (_REPO / "litellm_as_code" / "api.py").read_text(encoding="utf-8")
    extracted = digest.endpoints_from_client_source(source)
    assert extracted == set(digest.CONSUMED_ENDPOINTS)
    assert digest.DIGEST_EXCLUDED == {("GET", "/openapi.json")}
    # the full contract at the time of issue #26: 44 call-site pairs, of
    # which exactly one (/openapi.json, the version probe) is not digested
    assert len(digest.CONSUMED_ENDPOINTS) == 44


def test_extraction_handles_all_call_shapes():
    source = (
        # plain string, single-line
        'def a(self):\n    return self._request("POST", "/user/new", json=p)\n'
        # .get() chain directly after the call (no trailing comma)
        'def b(self):\n    return self._request("GET", "/credentials").get("credentials", [])\n'
        # multi-line
        'def c(self, x):\n    return self._request(\n        "DELETE", "/organization/delete",\n        json={"organization_ids": [x]},\n    )\n'
        # f-string with quote(...) placeholder
        'def d(self, name):\n    return self._request("PATCH", f"/credentials/{quote(name, safe=\'\')}", json=p)\n'
        # _paginated_list (implicitly GET) and the path-identifier inner call
        'def e(self):\n    return self._paginated_list("/user/list", "users", "total")\n'
        'def _paginated_list(self, path, list_key, total_key):\n'
        '    payload = self._request("GET", path, params=request_params)\n'
    )
    assert digest.endpoints_from_client_source(source) == {
        ("POST", "/user/new"),
        ("GET", "/credentials"),
        ("DELETE", "/organization/delete"),
        ("PATCH", "/credentials/{name}"),
        ("GET", "/user/list"),
    }


def test_extraction_refuses_unknown_shapes():
    with pytest.raises(ValueError):
        digest.endpoints_from_client_source('self._request(method="GET")\n')
    with pytest.raises(ValueError):
        digest.endpoints_from_client_source('self._request("GET", base_url + "/x")\n')


# ------------------------------------------------------------ path resolution


def test_spec_path_for_is_strict_segment_matching():
    paths = {
        "/credentials/{credential_name}": {},
        "/credentials/migrate-encryption": {},  # literal sibling
        "/guardrails/list": {},  # literal sibling
        "/guardrails/{guardrail_id}": {},
        "/guardrails/apply_guardrail": {},
        "/model/{model_id}/update": {},
    }
    # spec param name may differ from the client variable
    assert digest.spec_path_for("/credentials/{name}", paths) == "/credentials/{credential_name}"
    # placeholders never absorb literal segments
    assert digest.spec_path_for("/guardrails/{guardrail_id}", paths) == "/guardrails/{guardrail_id}"
    assert digest.spec_path_for("/model/{model_id}/update", paths) == "/model/{model_id}/update"
    # literal templates must match literally
    assert digest.spec_path_for("/guardrails/list", paths) == "/guardrails/list"
    # no match -> None
    assert digest.spec_path_for("/budget/list", paths) is None
    # ambiguity -> None (contract must be exact)
    assert digest.spec_path_for("/via/{param}", {"/via/{a}": {}, "/via/{b}": {}}) is None


# ---------------------------------------------------------------- normalization


def test_prune_drops_prose_keeps_contract(monkeypatch):
    payload = _mini(_spec(), {("POST", "/user/new")}, monkeypatch)
    entry = payload["endpoints"]["POST /user/new"]
    operation = entry["operation"]
    text = json.dumps(operation)
    # prose and churn keys are gone (installed at any depth)
    for noise in ("description", '"example"', "operationId", "x-internal", '"tags"'):
        assert noise not in text
    # contract keys survive
    assert operation["requestBody"]["required"] is True
    schema = operation["requestBody"]["content"]["application/json"]["schema"]
    # the requestBody $ref was inlined against components
    assert schema["type"] == "object" and schema["required"] == ["user_id"]
    assert schema["properties"]["user_id"]["type"] == "string"
    assert schema["properties"]["user_email"]["anyOf"][0]["type"] == "string"
    # response schemas keep their shape, minus prose in leaf properties
    response_schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert response_schema["properties"]["note"].get("description") is None


def test_ref_resolution_is_bounded_and_never_crashes(monkeypatch):
    payload = _mini(_spec(), {("POST", "/user/new")}, monkeypatch)
    schema = payload["endpoints"]["POST /user/new"]["operation"]["requestBody"][
        "content"
    ]["application/json"]["schema"]
    nested = schema["properties"]["nested"]
    # the self-reference recurses until the depth cap, which is a marked node
    assert "$ref-depth-cutoff" in json.dumps(nested)
    # unresolvable refs are marked, never KeyError
    assert schema["properties"]["missing"] == {"$ref-unresolved": "#/components/schemas/NotThere"}


def test_parameter_prose_is_pruned_but_contract_keys_survive(monkeypatch):
    payload = _mini(_spec(), {("PATCH", "/credentials/{name}")}, monkeypatch)
    parameters = payload["endpoints"]["PATCH /credentials/{name}"]["operation"]["parameters"]
    assert parameters == [
        {
            "in": "path",
            "name": "credential_name",
            "required": True,
            "schema": {"type": "string"},
        }
    ]
    properties = payload["endpoints"]["PATCH /credentials/{name}"]["operation"][
        "requestBody"
    ]["content"]["application/json"]["schema"]["properties"]
    assert "x-vendor" not in json.dumps(properties["credential_values"])


# ---------------------------------------------------------------- digest shape


def test_digest_inventory_with_full_contract_on_partial_spec(monkeypatch):
    """Endpoints the stack does not serve are recorded as absent."""
    payload = _mini(_spec(), {("POST", "/user/new"), ("GET", "/team/info")}, monkeypatch)
    key = "POST /user/new"
    assert key in payload["endpoints"] and "operation" in payload["endpoints"][key]
    assert payload["endpoints"]["GET /team/info"] == {"absent": True}
    assert payload["spec_version"] == "1.99.0"
    assert payload["harness"] == "openapi_digest"
    # openapi.json is never in the digest (its schema IS the spec)
    assert "GET /openapi.json" not in payload["endpoints"]


def test_digest_is_deterministic_and_byte_deterministic(monkeypatch):
    spec = _spec()
    first = digest.digest_json(digest.digest_payload(spec))
    # 1) same object again
    assert first == digest.digest_json(digest.digest_payload(spec))
    # 2) every dict re-inserted in reverse key order (semantically equal input)
    def reverse_keys(node):
        if isinstance(node, dict):
            return {key: reverse_keys(value) for key, value in sorted(node.items(), reverse=True)}
        if isinstance(node, list):
            return [reverse_keys(item) for item in node]
        return node

    shuffled = reverse_keys(spec)
    assert first == digest.digest_json(digest.digest_payload(shuffled))
    # 3) sha256 is a digest of the operation, not the whole blob
    operations = json.loads(first)["endpoints"]
    assert all("sha256" in entry or entry.get("absent") for entry in operations.values())


def test_sha_hashes_only_that_endpoint(monkeypatch):
    payload = _mini(_spec(), {("POST", "/user/new"), ("DELETE", "/guardrails/{guardrail_id}")}, monkeypatch)
    spec2 = json.loads(json.dumps(_spec()))  # same spec
    # touch only /user/new's description (a pruned key) — sha must NOT move
    spec2["paths"]["/user/new"]["post"]["description"] = "different prose"
    payload2 = digest.digest_payload(spec2)
    assert (
        payload["endpoints"]["POST /user/new"]["sha256"]
        == payload2["endpoints"]["POST /user/new"]["sha256"]
    )
    # touch /user/new's required flag — sha MUST move
    spec3 = json.loads(json.dumps(_spec()))
    spec3["paths"]["/user/new"]["post"]["requestBody"]["required"] = False
    payload3 = digest.digest_payload(spec3)
    assert (
        payload["endpoints"]["POST /user/new"]["sha256"]
        != payload3["endpoints"]["POST /user/new"]["sha256"]
    )


# ---------------------------------------------------------------- diff


def _payload_for(spec: dict, monkeypatch) -> dict:
    return _mini(
        spec,
        {("POST", "/user/new"), ("GET", "/team/info"), ("DELETE", "/guardrails/{guardrail_id}")},
        monkeypatch,
    )


def test_diff_reports_rename_add_remove_and_absence(monkeypatch):
    old = _payload_for(_spec(), monkeypatch)
    # mutate: rename a request property, drop /guardrails/{guardrail_id}
    # from the spec (the endpoint flips served -> absent for that router)
    new_spec = json.loads(json.dumps(_spec()))
    properties = new_spec["components"]["schemas"]["NewUserRequest"]["properties"]
    properties["user_email2"] = properties.pop("user_email")
    del new_spec["paths"]["/guardrails/{guardrail_id}"]
    new = _payload_for(new_spec, monkeypatch)

    report = digest.diff_digests(old, new)
    assert not report["identical"]
    assert report["removed"] == []
    assert any(
        line.startswith("DELETE /guardrails/{guardrail_id}: served -> absent")
        for line in report["absence_changed"]
    )
    changed = report["changed"]["POST /user/new"]
    rendered = digest.render_diff(report)
    assert "OpenAPI contract drift (1.99.0 -> 1.99.0)" in rendered
    assert any(".user_email:" in line for line in changed)
    assert any(".user_email2:" in line for line in changed)


def test_diff_identical_is_empty_and_flags_identical(monkeypatch):
    old = _payload_for(_spec(), monkeypatch)
    new = _payload_for(json.loads(json.dumps(_spec())), monkeypatch)
    report = digest.diff_digests(old, new)
    assert report["identical"]
    assert digest.render_diff(report) == ""
    # prose-only change in the SOURCE spec still yields identical digests
    noisier = json.loads(json.dumps(_spec()))
    noisier["paths"]["/user/new"]["post"]["description"] = "brand new prose"
    assert digest.diff_digests(old, _payload_for(noisier, monkeypatch))["identical"]


def test_diff_added_endpoints_and_change_cap(monkeypatch):
    old = _payload_for(_spec(), monkeypatch)
    new_spec = json.loads(json.dumps(_spec()))
    properties = new_spec["components"]["schemas"]["NewUserRequest"]["properties"]
    # a wildly different schema: > 20 leaf changes must be capped in the text
    properties["x"] = {"type": "string"}
    for index in range(25):
        properties[f"extra_{index}"] = {"type": f"string{index}"}
    new = _payload_for(new_spec, monkeypatch)
    report = digest.diff_digests(old, new)
    rendered = digest.render_diff(report)
    # the inlined ref expansions multiply the affected leaves — the per-
    # endpoint listing must be capped at 20 lines with an and-N-more tail
    assert re.search(r"… and \d+ more", rendered)

    # also verify the endpoint ADDED path (used when the client grows a call)
    report2 = digest.diff_digests(new, old)
    assert report2["added"] == []
    added_report = digest.diff_digests(old, dict(new, endpoints=dict(new["endpoints"], **{"PROBE /x": {"absent": True}})))
    assert added_report["added"] == ["PROBE /x"]


# ---------------------------------------------------------------- CLI


def _write_digest(path: Path, payload: dict) -> Path:
    path.write_text(digest.digest_json(payload), encoding="utf-8")
    return path


def test_cli_capture_writes_digest_and_lists_absent(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(digest, "fetch_spec", lambda *a, **k: _spec())
    monkeypatch.setattr(digest, "CONSUMED_ENDPOINTS", frozenset({("POST", "/user/new"), ("GET", "/team/info")}))
    out = tmp_path / "nested" / "digest.json"
    code = digest.main(["capture", "--base-url", "http://x", "--api-key", "k", "--out", str(out)])
    assert code == 0
    assert out.exists()
    loaded = json.loads(out.read_text(encoding="utf-8"))
    assert loaded["harness"] == "openapi_digest"
    printed = capsys.readouterr().out
    assert "captured 2 endpoints" in printed
    assert "GET /team/info" in printed  # the absent one is called out


def test_cli_capture_network_error_is_exit_1(monkeypatch, tmp_path):
    import urllib.error

    def boom(*args, **kwargs):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(digest, "fetch_spec", boom)
    code = digest.main(["capture", "--base-url", "http://x", "--api-key", "k", "--out", str(tmp_path / "d.json")])
    assert code == 1


def test_cli_diff_exit_codes(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(digest, "CONSUMED_ENDPOINTS", frozenset({("POST", "/user/new")}))
    old = _payload_for(_spec(), monkeypatch)
    same = _payload_for(json.loads(json.dumps(_spec())), monkeypatch)
    old_path = _write_digest(tmp_path / "old.json", old)
    same_path = _write_digest(tmp_path / "same.json", same)

    assert digest.main(["diff", "--old", str(old_path), "--new", str(same_path)]) == 0
    assert "digests identical" in capsys.readouterr().out

    drifted = json.loads(json.dumps(_spec()))
    properties = drifted["components"]["schemas"]["NewUserRequest"]["properties"]
    properties["renamed_away"] = properties.pop("user_id")
    new = _payload_for(drifted, monkeypatch)
    new_path = _write_digest(tmp_path / "new.json", new)
    assert digest.main(["diff", "--old", str(old_path), "--new", str(new_path)]) == 2
    text = capsys.readouterr().out
    assert "OpenAPI contract drift" in text and "renamed_away" in text

    text_out = tmp_path / "drift.txt"
    assert (
        digest.main(
            ["diff", "--old", str(old_path), "--new", str(new_path), "--text-out", str(text_out)]
        )
        == 2
    )
    assert "OpenAPI contract drift" in text_out.read_text(encoding="utf-8")

    # junk input -> exit 1 (not a crash)
    junk = tmp_path / "junk.json"
    junk.write_text("not json", encoding="utf-8")
    assert digest.main(["diff", "--old", str(junk), "--new", str(new_path)]) == 1
    foreign = tmp_path / "foreign.json"
    foreign.write_text('{"harness": "something-else"}', encoding="utf-8")
    assert digest.main(["diff", "--old", str(foreign), "--new", str(new_path)]) == 1


def test_digest_json_has_trailing_newline_and_sorted_keys(monkeypatch):
    payload = _mini(_spec(), {("POST", "/user/new")}, monkeypatch)
    text = digest.digest_json(payload)
    assert text.endswith("}\n")
    keys = [line.strip().split('"')[1] for line in text.splitlines() if line.startswith('  "')]
    assert keys == sorted(keys)
