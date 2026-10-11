"""Mock tests for the compat-matrix harness's pure helpers.

The harness lives under scripts/ (not a package) — import it by file path and
exercise only the pure functions (parse_versions, _extract_summary) so the
matrix runner itself is covered by the mock gate too; everything docker- or
pytest-driving is validated by actually running the harness (locally and in
the compat-matrix workflow).
"""

from __future__ import annotations

import importlib.util
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
