"""
ATSDetector — Phase 3 (ARCHITECTURE.md).

Detects which ATS platform a job application URL/page belongs to, cheapest
check first:

1. **URL pattern** (`detect_from_url`) — pure string matching, no network
   call at all. Covers the overwhelming majority of cases: Greenhouse,
   Lever, Workday, etc. are almost always used via their own subdomain
   (`boards.greenhouse.io/...`, `jobs.lever.co/...`).
2. **DOM / meta-tag fingerprint** (`detect_from_page`) — only reached when
   the URL alone is ambiguous (a company hosts the ATS embedded on its own
   custom careers domain). Requires an already-opened Playwright `Page` —
   `ATSDetector` never launches a browser itself for this tier; that's
   `automation/browser/browser_manager.py::BrowserManager`'s job, keeping
   detection logic and browser lifecycle management separate.
3. **Fallback** — `{"ats": "custom", "confidence": 0.1}` for unrecognized
   portals, which routes to `automation/ats/generic/generic_adapter.py` and,
   per the compliance decision table in ARCHITECTURE.md, never autopilot.

Usage:

    >>> ATSDetector.detect("https://boards.greenhouse.io/acme/jobs/12345")
    {"ats": "greenhouse", "confidence": 0.98, "method": "url_pattern"}

    >>> # Ambiguous custom domain — pass an already-opened page for tier 2.
    >>> ATSDetector.detect("https://careers.acme.com/apply/123", page=page)
    {"ats": "workday", "confidence": 0.75, "method": "dom_fingerprint"}

`detect_ats_for_url()` at the bottom is a convenience one-shot helper for
callers that don't already have an open page (e.g. a quick CLI check) — it
only launches a throwaway browser when tier 1 is inconclusive.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from playwright.sync_api import Error as PlaywrightError, Page, sync_playwright

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DetectionResult:
    ats: str
    confidence: float
    method: str  # "url_pattern" | "dom_fingerprint" | "meta_tag" | "fallback"

    def as_dict(self) -> dict:
        return {"ats": self.ats, "confidence": self.confidence, "method": self.method}


# --- Tier 1: URL substrings that unambiguously identify a platform. -------
URL_PATTERNS: dict[str, list[str]] = {
    "greenhouse": ["boards.greenhouse.io", "job-boards.greenhouse.io"],
    "lever": ["jobs.lever.co"],
    "workday": ["myworkdayjobs.com"],
    "smartrecruiters": ["jobs.smartrecruiters.com"],
    "taleo": ["taleo.net"],
    "icims": ["icims.com"],
    "ashby": ["jobs.ashbyhq.com"],
    "bamboohr": ["bamboohr.com/careers", "bamboohr.com/jobs"],
    "oracle_hcm": ["fa.oraclecloud.com", "oraclecloud.com/hcmui"],
}

# --- Tier 2: CSS selectors that identify each platform's own embed/widget,
# for when a company hosts the ATS on its own custom domain (tier 1 finds
# nothing). Order within each list doesn't matter — first match wins.
DOM_FINGERPRINTS: dict[str, list[str]] = {
    "greenhouse": ["#grnhse_app", "#application_form", "form#application-form"],
    "lever": [".lever-jobs-loader", ".application-form", '[data-qa="posting-apply-button"]'],
    "workday": ["[data-automation-id]", "[data-automation-widget]"],
    "smartrecruiters": ["#job-application-widget", "[data-sr-job]", ".job-sr"],
    "taleo": ['iframe[src*="taleo" i]', "#taleoHostedPage"],
    "icims": ['iframe[id*="icims" i]', "#icims_content_iframe"],
    "ashby": ['[id^="ashby_embed"]', "#ashby_embed"],
    "bamboohr": ["#BambooHR-ATS-board", ".bamboohr-ats"],
    "oracle_hcm": ['[class*="oracle-hcm" i]', "#oracleHcmRoot"],
}

# --- Tier 2b: <meta name="generator"> content — cheap to check once a page
# is already open, occasionally set by an ATS's embed script.
META_GENERATOR_HINTS: dict[str, list[str]] = {
    "greenhouse": ["greenhouse"],
    "lever": ["lever"],
    "smartrecruiters": ["smartrecruiters"],
}

FALLBACK_ATS = "custom"
FALLBACK_CONFIDENCE = 0.1
URL_MATCH_CONFIDENCE = 0.98
DOM_MATCH_CONFIDENCE = 0.75
META_MATCH_CONFIDENCE = 0.65


class ATSDetector:
    """Detects the ATS platform behind a job application URL/page. See
    module docstring for the tiered strategy."""

    @staticmethod
    def detect_from_url(url: str) -> DetectionResult | None:
        """Tier 1. Returns `None` (not the fallback) when nothing matches,
        so `detect()` knows to escalate to the DOM tier rather than give up."""
        normalized = url.lower()
        for platform, patterns in URL_PATTERNS.items():
            if any(pattern in normalized for pattern in patterns):
                return DetectionResult(ats=platform, confidence=URL_MATCH_CONFIDENCE, method="url_pattern")
        return None

    @staticmethod
    def detect_from_page(page: Page) -> DetectionResult | None:
        """Tier 2 — DOM fingerprint, then meta-tag, checks against an
        already-opened Playwright `Page`. Only worth calling when
        `detect_from_url` returned `None`."""
        for platform, selectors in DOM_FINGERPRINTS.items():
            for selector in selectors:
                try:
                    if page.locator(selector).count() > 0:
                        return DetectionResult(ats=platform, confidence=DOM_MATCH_CONFIDENCE, method="dom_fingerprint")
                except PlaywrightError as e:
                    # An invalid/unsupported selector on this particular page
                    # shouldn't abort detection for every other platform.
                    logger.debug("DOM fingerprint check failed for %s (%r): %s", platform, selector, e)
                    continue

        generator_content = None
        try:
            meta = page.locator('meta[name="generator"]').first
            if meta.count() > 0:
                generator_content = meta.get_attribute("content")
        except PlaywrightError:
            generator_content = None

        if generator_content:
            generator_lower = generator_content.lower()
            for platform, hints in META_GENERATOR_HINTS.items():
                if any(hint in generator_lower for hint in hints):
                    return DetectionResult(ats=platform, confidence=META_MATCH_CONFIDENCE, method="meta_tag")

        return None

    @classmethod
    def detect(cls, url: str, page: Page | None = None) -> dict:
        """Full detection flow — tier 1, then (if `page` is supplied) tier 2,
        then fallback. Returns a plain dict: `{"ats": ..., "confidence": ...,
        "method": ...}`, matching the shape from the project brief plus an
        extra `method` field for debugging/logging."""
        result = cls.detect_from_url(url)
        if result is not None:
            return result.as_dict()

        if page is not None:
            result = cls.detect_from_page(page)
            if result is not None:
                return result.as_dict()

        logger.info("Could not confidently detect an ATS platform for %s — routing to the generic adapter.", url)
        return DetectionResult(ats=FALLBACK_ATS, confidence=FALLBACK_CONFIDENCE, method="fallback").as_dict()


def detect_ats_for_url(url: str, *, headless: bool = True) -> dict:
    """Convenience one-shot detector for callers with no already-open page
    (e.g. a quick manual check). Tier 1 is tried first with **no** browser
    launch; a throwaway, unauthenticated browser is only opened if that's
    inconclusive. For repeated/production use inside the apply flow, prefer
    `BrowserManager` to open the page once and reuse it for both detection
    (`ATSDetector.detect(url, page)`) and the adapter's own work — this
    avoids paying browser-launch cost twice.
    """
    result = ATSDetector.detect_from_url(url)
    if result is not None:
        return result.as_dict()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        try:
            page = browser.new_page()
            page.goto(url, wait_until="domcontentloaded")
            return ATSDetector.detect(url, page)
        finally:
            browser.close()
