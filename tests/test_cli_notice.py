"""Tests for the CLI's author & license notice.

Verifies the notice is rendered exactly once on both command paths (reconcile
and export), is suppressible with `--quiet`, never contaminates stdout, and
stays in sync with `pyproject.toml` + `__version__` (the single sources of
truth).
"""

from __future__ import annotations

import re
from pathlib import Path

try:
    import tomllib  # Python >= 3.11
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

from litellm_as_code import __version__
from litellm_as_code._meta import AUTHOR, LICENSE
from litellm_as_code.cli import build_export_parser, build_parser, main
from litellm_as_code.notice import notice, print_notice

# Lenient about the dash: the notice renders a UTF-8 em-dash ("—"), but some
# CI/Windows consoles mangle it — accept either an em-dash or a double hyphen.
_NOTICE_RE = re.compile(
    r"^litellm-as-code [\w.]+ (?:—|--) by Rafael Kallis, licensed under MIT$",
    re.MULTILINE,
)


_REPO_ROOT = Path(__file__).resolve().parents[1]


def _read_pyproject() -> dict:
    """Load pyproject.toml resolved from this file's repo root, so the test is
    robust under any pytest invocation/cwd."""
    with open(_REPO_ROOT / "pyproject.toml", "rb") as f:
        return tomllib.load(f)


def test_notice_mentions_author_and_license():
    """The notice names the author and the license."""
    text = notice()
    assert "Rafael Kallis" in text
    assert "MIT" in text


def test_notice_includes_version_from_init():
    """Version in the notice matches __version__ (single source of truth)."""
    assert __version__ in notice()


def test_metadata_matches_pyproject():
    """_meta, pyproject.toml, and __init__ agree on one author/license."""
    project = _read_pyproject()["project"]
    (author,) = project["authors"]
    assert author["name"] == "Rafael Kallis"
    assert project["license"]["text"] == "MIT"
    assert project["version"] == __version__
    # the rendered string reflects the same facts
    text = notice()
    assert "Rafael Kallis" in text and "MIT" in text


def test_print_notice_goes_to_stderr(capsys):
    """The notice is printed to stderr so stdout stays machine-consumable."""
    print_notice()
    out, err = capsys.readouterr()
    assert out == ""
    assert _NOTICE_RE.search(err)


def test_both_parsers_surface_attribution_in_help():
    """`--help` on both command paths exposes author/license in the description
    (reconcile parser and export parser), matching the documented CLI surface.

    The attribution must come from the shared `_meta` constants (not
    hard-coded literals) so the help text can't drift from the notice / package
    metadata when they change."""
    for p in (build_parser(), build_export_parser()):
        assert AUTHOR in p.description
        assert LICENSE in p.description


def test_reconcile_path_prints_notice_once_on_stderr(capsys):
    """Even a failing (missing-credentials) reconcile run renders the notice
    once, on stderr, before reporting the error."""
    rc = main(["--dry-run"])  # missing --base-url / --api-key
    assert rc == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert len(_NOTICE_RE.findall(err)) == 1
    assert "error:" in err


def test_export_path_prints_notice_once_on_stderr(capsys):
    """The export subcommand also renders the notice once, on stderr."""
    rc = main(["export", "out.yml"])  # missing credentials -> exits 1
    assert rc == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert len(_NOTICE_RE.findall(err)) == 1
    assert "error:" in err


def test_quiet_suppresses_notice(capsys):
    """`--quiet` silences the notice (reconcile path) while keeping errors."""
    rc = main(["--dry-run", "--quiet"])
    assert rc == 1
    _, err = capsys.readouterr()
    assert "Rafael Kallis" not in err


def test_quiet_export_suppresses_notice(capsys):
    """`--quiet` silences the notice on the export subcommand too."""
    rc = main(["export", "out.yml", "--quiet"])
    assert rc == 1
    _, err = capsys.readouterr()
    assert "Rafael Kallis" not in err
