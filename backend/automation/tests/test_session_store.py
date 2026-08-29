"""
SessionStore (automation/browser/session.py) — encrypted per-(user, ats)
Playwright storage-state persistence. No real Playwright/browser needed:
`storage_state` is just a plain dict here, exactly the shape
`BrowserContext.storage_state()` returns.
"""

import json

from automation.browser.session import SessionStore

SAMPLE_STORAGE_STATE = {
    "cookies": [{"name": "session_id", "value": "abc123", "domain": "boards.greenhouse.io"}],
    "origins": [{"origin": "https://boards.greenhouse.io", "localStorage": [{"name": "x", "value": "y"}]}],
}


def test_save_then_load_round_trips(tmp_path):
    store = SessionStore(base_dir=tmp_path)

    store.save("user-1", "greenhouse", SAMPLE_STORAGE_STATE)
    loaded = store.load("user-1", "greenhouse")

    assert loaded == SAMPLE_STORAGE_STATE


def test_file_on_disk_is_encrypted_not_plaintext_json(tmp_path):
    store = SessionStore(base_dir=tmp_path)
    store.save("user-1", "greenhouse", SAMPLE_STORAGE_STATE)

    raw_bytes = next(tmp_path.iterdir()).read_text(encoding="utf-8")

    # The cookie value must never appear in plaintext on disk.
    assert "abc123" not in raw_bytes
    with_error = False
    try:
        json.loads(raw_bytes)
    except json.JSONDecodeError:
        with_error = True
    assert with_error, "session file should be Fernet ciphertext, not plain JSON"


def test_load_returns_none_when_no_session_saved(tmp_path):
    store = SessionStore(base_dir=tmp_path)
    assert store.load("nobody", "lever") is None


def test_has_session_reflects_save_and_delete(tmp_path):
    store = SessionStore(base_dir=tmp_path)
    assert store.has_session("user-1", "lever") is False

    store.save("user-1", "lever", SAMPLE_STORAGE_STATE)
    assert store.has_session("user-1", "lever") is True

    store.delete("user-1", "lever")
    assert store.has_session("user-1", "lever") is False
    assert store.load("user-1", "lever") is None


def test_sessions_are_isolated_per_user_and_platform(tmp_path):
    store = SessionStore(base_dir=tmp_path)
    store.save("user-1", "greenhouse", {"cookies": [{"name": "a"}]})
    store.save("user-1", "lever", {"cookies": [{"name": "b"}]})
    store.save("user-2", "greenhouse", {"cookies": [{"name": "c"}]})

    assert store.load("user-1", "greenhouse") == {"cookies": [{"name": "a"}]}
    assert store.load("user-1", "lever") == {"cookies": [{"name": "b"}]}
    assert store.load("user-2", "greenhouse") == {"cookies": [{"name": "c"}]}


def test_corrupt_ciphertext_is_treated_as_no_session(tmp_path):
    store = SessionStore(base_dir=tmp_path)
    store.save("user-1", "greenhouse", SAMPLE_STORAGE_STATE)

    path = store._path_for("user-1", "greenhouse")
    path.write_text("not-a-real-fernet-token", encoding="utf-8")

    assert store.load("user-1", "greenhouse") is None


def test_path_is_namespaced_and_survives_slashes_in_ids(tmp_path):
    store = SessionStore(base_dir=tmp_path)
    # Defensive: user_id/ats_platform are our own identifiers, but the path
    # helper should never let a stray separator escape the base directory.
    path = store._path_for("weird/id", "also/weird")
    assert path.parent == tmp_path
    assert "/" not in path.name.replace(".session.enc", "")
