"""
BrowserManager — Phase 2 (ARCHITECTURE.md).

Owns the Playwright lifecycle: launching a browser, holding one persistent,
encrypted storage-state per (user_id, ats_platform) via `SessionStore`,
capturing screenshots/traces on failure, and retrying transient navigation
errors. Uses Playwright's synchronous API (`playwright.sync_api`) since this
runs from synchronous worker/agent code (Celery/ARQ tasks, Phase 4+), not
inside FastAPI's async request path.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, TypeVar

from playwright.sync_api import (
    BrowserContext,
    Error as PlaywrightError,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from app.core.config import AUTOMATION_HEADLESS, AUTOMATION_LOGS_DIR
from automation.browser.session import SessionStore

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Transient Playwright errors worth retrying (navigation timeouts, detached
# frames, target-closed races) as opposed to logic errors in our own code.
_RETRYABLE_ERRORS: tuple[type[Exception], ...] = (PlaywrightTimeoutError, PlaywrightError)


class BrowserAutomationError(Exception):
    """Raised when a browser action fails after all retries, or when a
    BrowserManager method is used out of sequence (e.g. saving a session
    with no active context)."""


class BrowserManager:
    """Launches/reuses a persistent Playwright browser context for one
    (user_id, ats_platform) pair, and provides the cross-cutting operations
    every ATS adapter (Phase 3/4) needs: session persistence, retries,
    screenshots, and traces.

    One instance = one browser/context lifecycle. Typical use (once ATS
    adapters exist, Phase 4):

        manager = BrowserManager(user_id=user.user_id, ats_platform="greenhouse")
        with manager.session() as context:
            page = context.new_page()
            manager.run_with_retries(lambda: page.goto(job_url))
            ...
    """

    def __init__(
        self,
        user_id: str,
        ats_platform: str,
        headless: bool | None = None,
        session_store: SessionStore | None = None,
    ) -> None:
        self.user_id = user_id
        self.ats_platform = ats_platform
        self.headless = AUTOMATION_HEADLESS if headless is None else headless
        self._session_store = session_store or SessionStore()

        self._playwright = None
        self._browser = None
        self._context: BrowserContext | None = None

    # ------------------------------------------------------------------
    # Lifecycle: launch / reuse / close
    # ------------------------------------------------------------------

    def launch_context(self) -> BrowserContext:
        """Launches Chromium and a context for this (user_id, ats_platform),
        restoring an encrypted saved session if one exists. Idempotent per
        instance: a second call reuses the already-open context rather than
        launching a second browser."""
        if self._context is not None:
            return self._context

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=self.headless)

        storage_state = self._session_store.load(self.user_id, self.ats_platform)
        self._context = (
            self._browser.new_context(storage_state=storage_state)
            if storage_state
            else self._browser.new_context()
        )
        return self._context

    def new_page(self) -> Page:
        """Launches (or reuses) the context and opens a fresh page in it."""
        return self.launch_context().new_page()

    def has_saved_session(self) -> bool:
        return self._session_store.has_session(self.user_id, self.ats_platform)

    def save_session(self, context: BrowserContext | None = None) -> None:
        """Persists the context's current storage-state (cookies/local-storage),
        encrypted, for reuse on the next run (see `SessionStore`)."""
        context = context or self._context
        if context is None:
            raise BrowserAutomationError("No active browser context to save a session from.")
        self._session_store.save(self.user_id, self.ats_platform, context.storage_state())

    def close(self) -> None:
        """Closes context, browser, and the Playwright driver, in order.
        Best-effort: an error closing an already-closed resource is logged,
        not raised, so cleanup after a crash never masks the original error."""
        for closer in (
            lambda: self._context.close() if self._context else None,
            lambda: self._browser.close() if self._browser else None,
            lambda: self._playwright.stop() if self._playwright else None,
        ):
            try:
                closer()
            except PlaywrightError as e:
                logger.warning("Error during browser cleanup (ignored): %s", e)
        self._context = None
        self._browser = None
        self._playwright = None

    @contextmanager
    def session(self, *, persist: bool = True) -> Iterator[BrowserContext]:
        """Convenience context manager for ATS adapters:

            with browser_manager.session() as context:
                page = context.new_page()
                ...

        Saves the (possibly newly-authenticated) session on clean exit unless
        `persist=False`, and always closes the browser — even on error."""
        context = self.launch_context()
        try:
            yield context
            if persist:
                self.save_session(context)
        finally:
            self.close()

    # ------------------------------------------------------------------
    # Authentication: manual-login primitive (Phase 7 builds the user-facing
    # handoff; see ARCHITECTURE.md "No password harvesting" — only the
    # resulting cookies/local-storage are ever persisted, never a password)
    # ------------------------------------------------------------------

    @contextmanager
    def manual_login_session(self, url: str) -> Iterator[Page]:
        """Launches a NON-headless context (regardless of `self.headless`)
        navigated to `url`, for the user to log in by hand. On clean exit,
        the resulting session is saved via `SessionStore`. This method only
        provides the underlying Playwright primitive — the actual UI handoff
        to the user (e.g. a "watch and take over" flow) is Phase 7 work."""
        if self._context is not None:
            raise BrowserAutomationError("A context is already active on this BrowserManager instance.")

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=False)
        self._context = self._browser.new_context()
        page = self._context.new_page()
        try:
            page.goto(url)
            yield page
            self.save_session(self._context)
        finally:
            self.close()

    # ------------------------------------------------------------------
    # Retries
    # ------------------------------------------------------------------

    def run_with_retries(
        self,
        action: Callable[[], T],
        *,
        max_attempts: int = 3,
        backoff_base_seconds: float = 2.0,
        retry_on: tuple[type[Exception], ...] = _RETRYABLE_ERRORS,
    ) -> T:
        """Runs `action()` (a zero-arg callable, typically a small lambda
        wrapping one Playwright call), retrying on `retry_on` exceptions with
        exponential backoff. Mirrors the retry shape of
        `app/ai/llm/router.py::LLMRouter.run` elsewhere in this codebase, so
        the two "flaky external call" spots in the app behave consistently."""
        last_error: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                return action()
            except retry_on as e:
                last_error = e
                if attempt < max_attempts:
                    delay = backoff_base_seconds * (2 ** (attempt - 1))
                    logger.warning(
                        "Browser action attempt %d/%d failed: %s — retrying in %.1fs",
                        attempt, max_attempts, e, delay,
                    )
                    time.sleep(delay)

        raise BrowserAutomationError(
            f"Browser action failed after {max_attempts} attempts: {last_error}"
        ) from last_error

    # ------------------------------------------------------------------
    # Screenshots / traces / error log (§14 Logging and Debugging)
    # ------------------------------------------------------------------

    def _run_dir(self, application_id: str) -> Path:
        directory = Path(AUTOMATION_LOGS_DIR) / application_id
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def run_directory(self, application_id: str) -> Path:
        """This run's artifact directory (`logs/<application_id>/`), created if
        needed — the same one screenshots, the trace, and the error log go to.
        Public so a caller with its own artifact to write can put it alongside
        them instead of inventing a second location: the vision fallback pass
        saves the cropped screenshots it sent to the model here, which is the
        first thing anyone reviewing a questionable vision answer wants to
        see."""
        return self._run_dir(application_id)

    def screenshot_on_failure(self, page: Page, application_id: str) -> str:
        """Saves a full-page screenshot to `logs/<application_id>/screenshotN.png`
        (N auto-incremented) and returns its path — feeds
        `automation_runs.screenshot_paths`."""
        directory = self._run_dir(application_id)
        next_index = len(list(directory.glob("screenshot*.png"))) + 1
        path = directory / f"screenshot{next_index}.png"
        page.screenshot(path=str(path), full_page=True)
        return str(path)

    def start_trace(self, context: BrowserContext | None = None) -> None:
        context = context or self._context
        if context is None:
            raise BrowserAutomationError("No active browser context to trace.")
        context.tracing.start(screenshots=True, snapshots=True, sources=True)

    def stop_trace(self, application_id: str, context: BrowserContext | None = None) -> str:
        """Stops tracing and writes `logs/<application_id>/trace.zip`
        (viewable at https://trace.playwright.dev) — feeds
        `automation_runs.trace_path`."""
        context = context or self._context
        if context is None:
            raise BrowserAutomationError("No active browser context to trace.")
        path = self._run_dir(application_id) / "trace.zip"
        context.tracing.stop(path=str(path))
        return str(path)

    def write_error_log(self, application_id: str, message: str) -> str:
        """Appends `message` to `logs/<application_id>/error.log` and returns
        its path — feeds `automation_runs.error_log`."""
        path = self._run_dir(application_id) / "error.log"
        with open(path, "a", encoding="utf-8") as f:
            f.write(message.rstrip("\n") + "\n")
        return str(path)
