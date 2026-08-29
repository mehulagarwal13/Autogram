"""
Browser-level smoke test: does the PRODUCTION BUNDLE actually boot?

`vite build` succeeding proves the modules resolved and the syntax parsed. It
proves nothing about whether the app mounts: a component that throws on first
render, a missing provider, a bad import that only resolves at runtime, or a
crash in a top-level effect all build perfectly and then show the user a blank
white page. That failure is invisible to every other test in this repository —
the vitest suite renders components in isolation, and the Python E2E drives the
API without ever loading the UI.

So this test builds the real bundle, serves it, opens it in a real Chromium, and
asserts that React actually rendered something and that the console is free of
errors.

SCOPE, stated honestly: this is a boot smoke test, NOT the full UI lifecycle.
It runs the frontend WITHOUT a backend, so it exercises the unauthenticated
entry point only. A genuine end-to-end UI test — paste a URL, watch automation
run, answer a chat prompt, approve, submit — needs both servers plus a seeded
account and is not implemented; see the phase report.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[3] / "frontend"
DIST = FRONTEND / "dist"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


npm_available = pytest.mark.skipif(
    shutil.which("npm") is None, reason="npm not installed — cannot build the frontend bundle.",
)


@pytest.fixture(scope="module")
def built_bundle():
    """Build once for the module. Slow (~10s), and the whole point is to test
    the REAL production output rather than a dev server."""
    result = subprocess.run(
        ["npm", "run", "build"], cwd=FRONTEND, capture_output=True, text=True, shell=True, timeout=600,
    )
    if result.returncode != 0:
        pytest.fail(f"frontend build failed:\n{result.stdout}\n{result.stderr}")
    assert (DIST / "index.html").is_file(), "build produced no index.html"
    yield DIST
    shutil.rmtree(DIST, ignore_errors=True)


@pytest.fixture(scope="module")
def served(built_bundle):
    """A plain static server, deliberately not `vite preview`: fewer moving
    parts, and it is closer to how the bundle is actually deployed."""
    port = _free_port()

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(built_bundle), **kwargs)

        def log_message(self, *args):  # keep pytest output readable
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(0.3)
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


@npm_available
def test_the_built_app_mounts_without_console_errors(served, requires_chromium):
    """The failure this catches: a bundle that builds cleanly and then renders a
    blank page. Nothing else in the repo would notice."""
    from playwright.sync_api import sync_playwright

    console_errors: list[str] = []
    page_errors: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: page_errors.append(str(e)))

        page.goto(served, wait_until="networkidle")
        # React mounts into #root; if it threw during render this stays empty
        # even though the HTML document itself loaded fine.
        root_html = page.inner_html("#root")
        title = page.title()
        body_text = page.inner_text("body")
        browser.close()

    assert not page_errors, f"the app threw during render: {page_errors}"
    # Failed API calls are EXPECTED here (no backend is running) and are not a
    # boot failure. Anything else is.
    real_errors = [
        e for e in console_errors
        if "Failed to load resource" not in e and "net::ERR" not in e and "/api/" not in e
    ]
    assert not real_errors, f"console errors on boot: {real_errors}"
    assert root_html.strip(), "#root is empty — React did not render anything"
    assert body_text.strip(), "the page rendered no visible text"
    assert title, "the document has no title"


@npm_available
def test_the_bundle_contains_no_hardcoded_secret(built_bundle):
    """Everything in `dist/` is shipped to the browser. An API key or database
    URL that reached a `VITE_`-prefixed variable would be baked into the bundle
    in plain text and served to every visitor.
    """
    suspicious = ("sk-", "postgresql://", "neon.tech", "OPENAI_API_KEY", "JWT_SECRET", "ENCRYPTION_KEY")
    for asset in built_bundle.rglob("*"):
        if not asset.is_file() or asset.suffix not in (".js", ".css", ".html", ".map"):
            continue
        text = asset.read_text(encoding="utf-8", errors="ignore")
        for needle in suspicious:
            assert needle not in text, f"{asset.name} contains {needle!r} — a secret is in the shipped bundle"
