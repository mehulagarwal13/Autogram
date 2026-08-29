"""
Test bootstrap for `automation/tests/`.

`automation/` is now an internal module of the same application as `app/`
(see `automation/interfaces.py` and `automation/README.md`) — `automation.browser.session`
and `automation.ats.base` (via `automation.interfaces`) import `app.core.config`,
`app.core.crypto`, etc., which fail fast on missing env vars. This mirrors
`tests/conftest.py`'s bootstrap exactly, duplicated here (rather than
imported across the sibling `tests/` package) so `automation/tests/` passes
whether it's run standalone (`pytest automation/tests/`) or as part of the
whole suite (`pytest`), regardless of which directory pytest collects first.
"""

import os

import pytest
from playwright.sync_api import Error as PlaywrightError, sync_playwright

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("ADZUNA_APP_ID", "test-id")
os.environ.setdefault("ADZUNA_APP_KEY", "test-key")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret")
# Same test-only Fernet key as tests/conftest.py — never used against real data.
os.environ.setdefault("ENCRYPTION_KEY", "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=")

# Human-paced input (`automation/utils/human_input.py`) is ON by default for
# real runs — scroll/settle/click/type-per-character plus a 2s pause after each
# field. Off here: the suite fills hundreds of fields, and 2s apiece alone
# would add well over an hour. `test_human_input.py` re-enables it explicitly
# for the handful of tests that assert on the pacing itself.
os.environ.setdefault("AUTOMATION_HUMAN_PACING", "0")


# ---------------------------------------------------------------------------
# Shared Playwright fixtures for every test module under automation/tests/
# that needs a real rendered page (test_selectors.py, test_detector.py, ...).
# Session-scoped so the whole suite launches Chromium once, not once per file.
# Skips with a clear message (not an error) if Chromium isn't installed —
# that's an environment setup step (`playwright install chromium`), not a
# code bug.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as p:
        try:
            b = p.chromium.launch(headless=True)
        except PlaywrightError as e:
            pytest.skip(f"Chromium not installed for Playwright — run `playwright install chromium`: {e}")
            return
        yield b
        b.close()


@pytest.fixture
def page(browser):
    p = browser.new_page()
    yield p
    p.close()


@pytest.fixture
def requires_chromium():
    """Skips the test if Chromium isn't installed for Playwright — same
    check as `browser` above, but its OWN `sync_playwright()` context is
    fully entered AND exited before the test body ever runs (no `yield`
    inside the `with` block), so no live Playwright driver connection is
    left open on this thread afterwards.

    Use this instead of `browser` in any test that builds its own,
    independent `BrowserManager`/`ApplicationFlowManager` (which starts its
    own separate `sync_playwright()` instance) rather than using the
    fixture's `Browser`/`Page` object directly. `sync_playwright().start()`
    refuses outright ("...you are using Playwright Sync API inside the
    asyncio loop...") whenever ANOTHER `sync_playwright()` context is still
    open on the SAME thread — and the session-scoped `browser` fixture's
    `with sync_playwright() as p:` block stays open for the entire test
    session once any test requests it, which is exactly that situation for
    every later test on this (single, sequential) pytest thread that tries
    to launch its own separate Playwright instance."""
    try:
        with sync_playwright() as p:
            p.chromium.launch(headless=True).close()
    except PlaywrightError as e:
        if "asyncio loop" in str(e):
            # Not actually a missing-Chromium environment issue — some
            # earlier test in this session already activated the `browser`
            # fixture (its `with sync_playwright() as p:` stays open for the
            # whole session once anything requests it), and this fixture's
            # own separate `sync_playwright()` call collided with it on the
            # same thread. Failing loudly here (not skipping) is what keeps
            # this from silently masking as "Chromium isn't installed."
            pytest.fail(
                "requires_chromium collided with an already-open sync_playwright() "
                "context on this thread — something using the `browser`/`page` "
                "fixture ran earlier in this session. Run this test file on its "
                f"own, or before any file using `browser`/`page`. Original error: {e}"
            )
        pytest.skip(f"Chromium not installed for Playwright — run `playwright install chromium`: {e}")
