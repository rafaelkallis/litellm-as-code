"""Tests for the CLI's --help text (issue #13).

`--help` is what users see in the field — it must name EVERY env alias an
option actually reads, not just the primary one. AGENTS.md §6 (CLI contract)
and the README's CLI mirror document both alias spellings; these tests keep
the real help output in sync with that (env-precedence surprises are a
classic support sink).
"""

from __future__ import annotations

import pytest

from litellm_as_code.cli import build_export_parser, build_parser, main


def _flatten(text: str) -> str:
    """Normalize argparse's line wrapping to single-space text.

    The 78-col HelpFormatter can break inside an alias pair (e.g. `env:
    LITELLM_BASE_URL /` + newline + `BASE_URL)`), so assertions run against
    the join of all whitespace runs — equivalent to what the user reads.
    """
    return " ".join(text.split())


def test_reconcile_help_lists_both_env_aliases():
    """The apply parser's help names both aliases for each env-reading option."""
    text = _flatten(build_parser().format_help())
    assert "env: LITELLM_SPEC" in text
    assert "env: LITELLM_BASE_URL / BASE_URL" in text
    assert "env: LITELLM_API_KEY / API_KEY" in text


def test_export_help_lists_both_env_aliases():
    """Same for the export parser — including the `out` positional's
    LITELLM_SPEC alias (issue #13 bullet 3, already satisfied in code)."""
    text = _flatten(build_export_parser().format_help())
    assert "env: LITELLM_SPEC" in text
    assert "env: LITELLM_BASE_URL / BASE_URL" in text
    assert "env: LITELLM_API_KEY / API_KEY" in text


def test_main_help_output_lists_both_env_aliases(capsys):
    """End-to-end: the actual `--help` stdout (the notice is suppressed for
    -h/--help, so the capture is uncontaminated) carries every alias."""
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code in (0, None)
    out = _flatten(capsys.readouterr().out)
    assert "env: LITELLM_SPEC" in out
    assert "env: LITELLM_BASE_URL / BASE_URL" in out
    assert "env: LITELLM_API_KEY / API_KEY" in out
