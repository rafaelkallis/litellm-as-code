"""Mock tests for the compat-matrix harness's pure helpers.

The harness lives under scripts/ (not a package) — import it by file path and
exercise only the pure functions (parse_versions, _extract_summary) so the
matrix runner itself is covered by the mock gate too; everything docker- or
pytest-driving is validated by actually running the harness (locally and in
the compat-matrix workflow).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_HARNESS_PATH = Path(__file__).resolve().parents[1] / "scripts" / "compat_matrix.py"
spec = importlib.util.spec_from_file_location("compat_matrix", _HARNESS_PATH)
assert spec.loader is not None  # file-path import: loader is always present
compat_matrix = importlib.util.module_from_spec(spec)
sys.modules["compat_matrix"] = compat_matrix  # dataclass processing resolves by module name
spec.loader.exec_module(compat_matrix)


def test_parse_versions_splits_commas_spaces_and_dedupes():
    assert compat_matrix.parse_versions("v1.97.0, v1.104.2  v1.97.0") == [
        "v1.97.0",
        "v1.104.2",
    ]


def test_parse_versions_rejects_malformed_or_empty_requests():
    for bad in ("1.97.0", "v1", "v1.97.x", "latest", "v1.97.0,bogus", ""):
        with pytest.raises(ValueError):
            compat_matrix.parse_versions(bad)


def test_extract_summary_parses_the_pytest_summary_line():
    counts, no_tests = compat_matrix._extract_summary(".. output ..\n6 passed, 4 skipped in 1.2s\n")
    assert counts == {"passed": 6, "failed": 0, "errors": 0, "skipped": 4}
    assert no_tests is False


def test_extract_summary_counts_failures_and_errors():
    counts, _ = compat_matrix._extract_summary("1 failed, 3 passed, 1 error in 0.5s")
    assert counts == {"passed": 3, "failed": 1, "errors": 1, "skipped": 0}


def test_extract_summary_flags_a_vacuous_leg():
    counts, no_tests = compat_matrix._extract_summary("collected 9 items\nno tests ran")
    assert counts == {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    assert no_tests is True


# ------------------------------------------------------------------ report
# (--report-out: the JSON contract scripts/compat_canary.py consumes —
# tests/test_compat_canary.py validates the consumer side of the same schema.)


def _leg(**overrides) -> compat_matrix.LegResult:
    base = dict(
        version="v1.105.0",
        green=True,
        note="",
        exit_code=0,
        counts={"passed": 9, "failed": 0, "errors": 0, "skipped": 4},
        no_tests=False,
        tail="",
    )
    base.update(overrides)
    return compat_matrix.LegResult(**base)


def test_report_payload_round_trips_leg_data_and_works_for_fail_fast():
    red_leg = _leg(green=False, note="pytest exited 1", exit_code=1, tail="boom")
    payload = compat_matrix.report_payload(
        ["v1.97.0", "v1.105.0"], [_leg(version="v1.97.0"), red_leg], []
    )
    assert payload["all_green"] is False
    assert payload["ran_all"] is True
    assert [leg["version"] for leg in payload["legs"]] == ["v1.97.0", "v1.105.0"]
    assert payload["legs"][1]["tail"] == "boom"
    assert payload["not_run"] == []
    # fail-fast cut: the not-run versions are reported separately
    cut = compat_matrix.report_payload(["v1.97.0", "v1.105.0"], [], ["v1.105.0"])
    assert cut["all_green"] is False  # no results at all is never "green"
    assert cut["ran_all"] is False
    assert cut["not_run"] == ["v1.105.0"]


def test_report_payload_is_byte_deterministic_for_identical_matrices():
    payload = compat_matrix.report_payload(["v1.97.0"], [_leg(version="v1.97.0")], [])
    again = compat_matrix.report_payload(["v1.97.0"], [_leg(version="v1.97.0")], [])
    first = json.dumps(payload, sort_keys=True, indent=2) + "\n"
    second = json.dumps(again, sort_keys=True, indent=2) + "\n"
    assert first == second  # what write_report writes; no diff churn between runs


def test_write_report_creates_the_directory_and_writes_the_payload(tmp_path):
    target = tmp_path / "nested" / "report.json"
    compat_matrix.write_report(target, compat_matrix.report_payload(["v1.97.0"], [], []))
    assert json.loads(target.read_text(encoding="utf-8"))["harness"] == "compat_matrix"
    assert target.read_text(encoding="utf-8").endswith("\n")  # trailing newline


def test_parser_exposes_report_out_and_defaults_to_none():
    parser = compat_matrix.build_parser()
    defaults = parser.parse_args([])
    assert defaults.report_out is None
    explicit = parser.parse_args(["--report-out", "reports/nightly.json"])
    assert str(explicit.report_out) == "reports/nightly.json"


# ------------------------------------------------------------------ digest
# (--digest-out: per-leg OpenAPI contract captures — scripts/openapi_digest.py;)


def _canned_digest(version: str, spec_version: str = "1.97.0") -> dict:
    return {
        "version": version,
        "digest": {
            "harness": "openapi_digest",
            "format": 1,
            "spec_version": spec_version,
            "endpoints": {"GET /user/list": {"absent": True}},
        },
    }


def test_run_matrix_writes_digest_document_per_leg(monkeypatch, tmp_path):
    calls: list[str] = []

    def fake_leg(index, total, version, *args, **kwargs):
        calls.append(version)
        return _leg(version=version)

    monkeypatch.setattr(compat_matrix, "run_leg", fake_leg)
    monkeypatch.setattr(
        compat_matrix,
        "digest_after_leg",
        lambda port, key, version: _canned_digest(version),
    )
    monkeypatch.setattr(
        compat_matrix, "_compose", lambda env, *a, **k: type("P", (), {"returncode": 0})()
    )
    target = tmp_path / "digest-report.json"
    code = compat_matrix.run_matrix(
        port=4001,
        versions=["v1.97.0", "v1.105.0"],
        master_key="k",
        extra_env={},
        pytest_extra=[],
        keep=False,
        fail_fast=False,
        digest_out=target,
    )
    assert code == 0
    assert calls == ["v1.97.0", "v1.105.0"]
    document = json.loads(target.read_text(encoding="utf-8"))
    assert document["harness"] == "openapi_digest_report"
    assert [leg["version"] for leg in document["legs"]] == ["v1.97.0", "v1.105.0"]
    assert document["legs"][0]["digest"]["spec_version"] == "1.97.0"


def test_digest_report_is_byte_deterministic(monkeypatch, tmp_path):
    monkeypatch.setattr(compat_matrix, "run_leg", lambda *a, **k: _leg())
    monkeypatch.setattr(
        compat_matrix, "digest_after_leg", lambda port, key, version: _canned_digest(version)
    )
    monkeypatch.setattr(
        compat_matrix, "_compose", lambda env, *a, **k: type("P", (), {"returncode": 0})()
    )
    one = tmp_path / "a.json"
    two = tmp_path / "b.json"
    for target in (one, two):
        compat_matrix.run_matrix(
            port=4001, versions=["v1.97.0"], master_key="k", extra_env={},
            pytest_extra=[], keep=False, fail_fast=False, digest_out=target,
        )
    assert one.read_bytes() == two.read_bytes()


def test_digest_capture_failure_is_recorded_not_fatal(monkeypatch, tmp_path):
    monkeypatch.setattr(compat_matrix, "run_leg", lambda *a, **k: _leg())
    monkeypatch.setattr(
        compat_matrix,
        "digest_after_leg",
        lambda port, key, version: {
            "version": version,
            "capture_error": "URLError: connection refused",
        },
    )
    monkeypatch.setattr(
        compat_matrix, "_compose", lambda env, *a, **k: type("P", (), {"returncode": 0})()
    )
    target = tmp_path / "digest.json"
    compat_matrix.run_matrix(
        port=4001, versions=["v1.97.0"], master_key="k", extra_env={},
        pytest_extra=[], keep=False, fail_fast=False, digest_out=target,
    )
    document = json.loads(target.read_text(encoding="utf-8"))
    assert document["legs"][0]["capture_error"].startswith("URLError")
    assert "digest" not in document["legs"][0]


def test_digest_after_leg_swallows_fetch_errors(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("proxy vanished")

    monkeypatch.setattr(compat_matrix._digest, "fetch_spec", boom)
    entry = compat_matrix.digest_after_leg(4001, "k", "v1.97.0")
    assert entry["capture_error"].startswith("RuntimeError: proxy vanished")


def test_parser_exposes_digest_out_and_defaults_to_none():
    parser = compat_matrix.build_parser()
    assert parser.parse_args([]).digest_out is None
    explicit = parser.parse_args(["--digest-out", "reports/digest.json"])
    assert str(explicit.digest_out) == "reports/digest.json"
