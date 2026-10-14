"""Lint + generator units for the COMPAT.md checked content (issue #30).

The generator lives under scripts/ (not a package) — import it by file path,
like the other script harness tests. ``check()`` IS the lint the CI suite
runs; the unit tests below cover its inputs (workflow pins, sidecar facts,
evidence markers) plus the staleness semantics, so a failing check always
has a precise counterpart here.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_HARNESS_PATH = Path(__file__).resolve().parents[1] / "scripts" / "compat_doc.py"
spec = importlib.util.spec_from_file_location("compat_doc", _HARNESS_PATH)
assert spec.loader is not None  # file-path import: loader is always present
compat_doc = importlib.util.module_from_spec(spec)
sys.modules["compat_doc"] = compat_doc  # dataclass processing resolves by module name
spec.loader.exec_module(compat_doc)


# --- the lint itself (the contract tests/test_* actually gate CI on) --------


def test_lint_is_clean():
    """The checked content is in sync: rendered blocks byte-match, every
    evidence ref resolves, floors agree, links resolve, sidecar parses."""
    assert compat_doc.check() == []


def test_build_is_idempotent():
    """build() is a no-op when the file is current — and never rewrites it.
    This is exactly what the nightly's refresh step runs (together with the
    lint) when it commits the refreshed sidecar."""
    before = compat_doc.COMPAT_PATH.read_bytes()
    assert compat_doc.build() is False
    assert compat_doc.check() == []
    assert compat_doc.COMPAT_PATH.read_bytes() == before


# --- staleness semantics (no repo file mutations; the linter takes text) ---


def _verified_block_text() -> str:
    text = compat_doc.COMPAT_PATH.read_text(encoding="utf-8")
    start = text.index(compat_doc.BEGIN_VERIFIED) + len(compat_doc.BEGIN_VERIFIED)
    stop = text.index(compat_doc.END_VERIFIED)
    return text[start:stop]


def test_lint_flags_a_stale_verified_block():
    facts = compat_doc.sidecar_facts()
    stale = _verified_block_text().replace(facts["version"], "v0.0.0-stale")
    problems: list[str] = []
    compat_doc._check_generated_blocks(
        compat_doc.COMPAT_PATH.read_text(encoding="utf-8").replace(
            _verified_block_text(), stale
        ),
        problems,
    )
    assert any("verified block is stale" in p for p in problems)


def test_lint_flags_missing_block_markers():
    text = compat_doc.COMPAT_PATH.read_text(encoding="utf-8").replace(
        compat_doc.BEGIN_CAVEATS, "<!-- gone -->"
    )
    problems: list[str] = []
    compat_doc._check_generated_blocks(text, problems)
    assert any("markers missing" in p for p in problems)


def test_replace_block_normally_reframes_the_content():
    """The builder inserts newlines between markers and the content, and the
    staleness compare tolerates exactly that framing — nothing else."""
    text = "A<!-- BEGIN --><-- old -->\n<!-- END -->B"
    begin, end, content = ("<!-- BEGIN -->", "<!-- END -->", "new body")
    new, between = compat_doc._replace_block(text, begin, end, content)
    assert new == "A<!-- BEGIN -->\nnew body\n<!-- END -->B"
    assert between.strip("\n") != content  # the stale block IS flagged
    assert between == "<-- old -->\n"


def test_lint_flags_a_dead_evidence_marker(monkeypatch):
    """A refactor that deletes a caveat's marker must be caught with a
    precise message naming the caveat, file and marker."""
    import dataclasses

    stale = dataclasses.replace(
        compat_doc.CAVEATS[0],
        refs=(("litellm_as_code/capabilities.py", "marker that never existed"),),
    )
    monkeypatch.setattr(compat_doc, "CAVEATS", (stale,))
    problems: list[str] = []
    compat_doc._check_evidence_refs(problems)
    assert len(problems) == 1
    assert problems[0].startswith(
        "caveat costs-persist-dropped: evidence marker no longer in"
        " litellm_as_code/capabilities.py:"
    )


def test_lint_flags_a_missing_evidence_file(monkeypatch):
    import dataclasses

    moved = [
        dataclasses.replace(c, refs=(("gone/renamed.py", "marker"),))
        for c in compat_doc.CAVEATS
    ]
    monkeypatch.setattr(compat_doc, "CAVEATS", tuple(moved))
    problems: list[str] = []
    compat_doc._check_evidence_refs(problems)
    assert problems and all("evidence file missing" in p for p in problems)


# --- derived facts ----------------------------------------------------------


def test_workflow_pins_parse_and_agree():
    pins = compat_doc.publish_pins()
    assert pins["pypi_pin"] == "v1.97.0"
    assert pins["image_pin"] == "v1.97.0"
    assert pins["nightly_floor"] == "v1.97.0"
    legs = [v.strip() for v in (pins["matrix_legs"] or "").split(",") if v.strip()]
    assert legs[0] == "v1.97.0"
    assert len(legs) >= 2


def test_sidecar_facts_parse():
    facts = compat_doc.sidecar_facts()
    assert set(facts) == {"captured_at", "version", "run_url"}
    assert facts["version"].startswith("v")
    assert facts["run_url"].startswith("https://github.com/rafaelkallis/")


def test_lint_flags_disagreeing_floors(tmp_path, monkeypatch):
    wf = tmp_path / "publish-pypi.yml"
    wf.write_text("  LITELLM_VERSION: v1.96.0\n", encoding="utf-8")
    monkeypatch.setattr(compat_doc, "WF_PUBLISH_PYPI", wf)
    problems: list[str] = []
    compat_doc._check_floors_agree(problems)
    assert any("floor pins disagree" in p for p in problems)


def test_rendered_verified_derives_from_the_sidecar():
    render = compat_doc.render_verified()
    facts = compat_doc.sidecar_facts()
    assert facts["captured_at"] in render
    assert f"`{facts['version']}`" in render
    assert facts["run_url"] in render
    pins = compat_doc.publish_pins()
    floor = pins["nightly_floor"] or pins["pypi_pin"] or "?"
    assert f"`{floor}`" in render


def test_rendered_caveats_contains_every_row_and_link():
    render = compat_doc.render_caveats()
    for c in compat_doc.CAVEATS:
        assert c.name in render, c.id
        for path, marker in c.refs:
            assert f"`{marker}`]({path})" in render, c.id
        for text, url in c.links:
            assert f"[{text}]({url})" in render, c.id
    assert "evidence" in render
    assert "| caveat | affects | behavior | absorbed as | evidence |" in render


def test_every_caveat_has_evidence():
    for c in compat_doc.CAVEATS:
        assert c.refs or c.links, f"caveat {c.id} cites no evidence"


def test_filing_status_table_lists_all_four_candidates():
    """issue #30's four filings are prepared (pending) in FILINGS.md — each
    once in the status table and once in its own body section."""
    filings = compat_doc.FILINGS_PATH.read_text(encoding="utf-8")
    assert filings.count("| prepared (pending) |") == 4
    assert filings.count("**Status:** prepared (pending)") == 4
    assert "Per-deployment cost persistence" in filings
    assert "page_size" in filings
    assert "/organization/*" in filings
    assert "ghost user row" in filings


def test_cli_check_exit_codes_and_argv_contract():
    assert compat_doc.main(["check"]) == 0
    assert compat_doc.main([]) == 0  # default command is check
    assert compat_doc.main(["bogus"]) == 2
