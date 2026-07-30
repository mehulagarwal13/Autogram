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
