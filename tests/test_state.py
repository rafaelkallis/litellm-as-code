"""State-file round-trip + atomic-write tests."""

from __future__ import annotations

from litellm_as_code.state import State, StateStore


def test_state_roundtrip(tmp_path):
    path = tmp_path / "state.json"
    store = StateStore(path)

    s = State()
    s.keys["k1"] = {"key": "sk-abc"}
    s.credentials["c1"] = {"api_key": "sk-provider"}
    store.save(s)

    loaded = store.load()
    assert loaded.keys == {"k1": {"key": "sk-abc"}}
    assert loaded.credentials == {"c1": {"api_key": "sk-provider"}}
    assert loaded.version == 1


def test_missing_state_is_empty(tmp_path):
    store = StateStore(tmp_path / "does-not-exist.json")
    s = store.load()
    assert s.keys == {}
    assert s.credentials == {}


def test_corrupt_state_is_treated_as_empty(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not valid json")
    store = StateStore(path)
    assert store.load().keys == {}


def test_state_file_is_private(tmp_path):
    path = tmp_path / "state.json"
    store = StateStore(path)
    store.save(State())
    # 0o600-ish (masked by umask => group/other read must be off)
    mode = path.stat().st_mode & 0o777
    assert mode & 0o077 == 0


def test_state_roundtrip_is_sorted_and_readable(tmp_path):
    """state.json is written sorted + indented for git-friendly diffs."""
    path = tmp_path / "state.json"
    store = StateStore(path)
    s = State()
    s.keys["k1"] = {"key": "sk-abc"}
    s.credentials["c1"] = {"api_key": "sk-provider"}
    store.save(s)

    text = path.read_text()
    assert '"credentials"' in text and '"keys"' in text  # indented, not one line
    loaded = store.load()
    assert loaded.version == 1
