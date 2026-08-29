"""
ATSDetector (automation/ats/detector.py). The URL tier (`detect_from_url`)
needs no browser at all — tested directly. The DOM/meta tier
(`detect_from_page`) and the `detect_ats_for_url` convenience helper are
tested against real rendered pages via the shared `browser`/`page` fixtures
in `conftest.py` (skipped with a clear message if Chromium isn't installed).
`data:` URLs are used wherever a real page load is needed, so nothing here
touches the network.
"""

import pytest
from playwright.sync_api import Error as PlaywrightError

from automation.ats.detector import (
    URL_PATTERNS,
    ATSDetector,
    detect_ats_for_url,
)

# ---------- tier 1: URL pattern (no browser needed) ----------

REAL_WORLD_URLS = {
    "greenhouse": "https://boards.greenhouse.io/acme/jobs/12345",
    "lever": "https://jobs.lever.co/acme/abcd-1234",
    "workday": "https://acme.wd5.myworkdayjobs.com/en-US/External/job/123",
    "smartrecruiters": "https://jobs.smartrecruiters.com/Acme/12345-backend-engineer",
    "taleo": "https://acme.taleo.net/careersection/2/jobdetail.ftl?job=12345",
    "icims": "https://careers-acme.icims.com/jobs/1234/job",
    "ashby": "https://jobs.ashbyhq.com/acme/abcd-1234",
    "bamboohr": "https://acme.bamboohr.com/careers/123",
    "oracle_hcm": "https://acme.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/123",
}


@pytest.mark.parametrize("platform,url", list(REAL_WORLD_URLS.items()))
def test_url_tier_detects_every_platform(platform, url):
    result = ATSDetector.detect(url)
    assert result["ats"] == platform
    assert result["confidence"] == 0.98
    assert result["method"] == "url_pattern"


def test_url_tier_is_case_insensitive():
    result = ATSDetector.detect("https://BOARDS.GREENHOUSE.IO/Acme/Jobs/123")
    assert result["ats"] == "greenhouse"


def test_url_tier_returns_none_for_unrecognized_domain():
    assert ATSDetector.detect_from_url("https://careers.some-random-company.com/apply/1") is None


def test_detect_falls_back_to_custom_with_no_page_supplied():
    result = ATSDetector.detect("https://careers.some-random-company.com/apply/1")
    assert result == {"ats": "custom", "confidence": 0.1, "method": "fallback"}


def test_url_patterns_table_matches_every_supported_platform():
    assert set(URL_PATTERNS.keys()) == set(REAL_WORLD_URLS.keys())


# ---------- tier 2: DOM fingerprint / meta tag (needs a real page) ----------

CUSTOM_DOMAIN_URL = "https://careers.some-random-company.com/apply/1"


def test_dom_tier_detects_workday_fingerprint(page):
    page.set_content('<html><body><div data-automation-id="jobPostingHeader"></div></body></html>')
    result = ATSDetector.detect(CUSTOM_DOMAIN_URL, page=page)
    assert result == {"ats": "workday", "confidence": 0.75, "method": "dom_fingerprint"}


def test_dom_tier_detects_greenhouse_embed(page):
    page.set_content('<html><body><div id="grnhse_app"></div></body></html>')
    result = ATSDetector.detect(CUSTOM_DOMAIN_URL, page=page)
    assert result["ats"] == "greenhouse"
    assert result["method"] == "dom_fingerprint"


def test_meta_generator_tier_detects_lever(page):
    page.set_content('<html><head><meta name="generator" content="Lever Postings Widget"></head><body></body></html>')
    result = ATSDetector.detect(CUSTOM_DOMAIN_URL, page=page)
    assert result == {"ats": "lever", "confidence": 0.65, "method": "meta_tag"}


def test_dom_tier_falls_back_to_custom_when_page_has_no_fingerprint(page):
    page.set_content("<html><body><form><input type='text' name='email'></form></body></html>")
    result = ATSDetector.detect(CUSTOM_DOMAIN_URL, page=page)
    assert result == {"ats": "custom", "confidence": 0.1, "method": "fallback"}


def test_url_tier_wins_even_when_page_fingerprint_disagrees(page):
    # A greenhouse.io URL is decisive on its own — the (irrelevant) page
    # content shouldn't even be consulted.
    page.set_content('<html><body><div data-automation-id="unrelated-workday-fingerprint"></div></body></html>')
    result = ATSDetector.detect("https://boards.greenhouse.io/acme/jobs/123", page=page)
    assert result["ats"] == "greenhouse"
    assert result["method"] == "url_pattern"


# ---------- detect_ats_for_url convenience helper ----------

def test_detect_ats_for_url_skips_browser_entirely_for_url_tier_hit(monkeypatch):
    def _fail_if_called(*args, **kwargs):
        raise AssertionError("sync_playwright() should not be called when the URL tier already matched")

    monkeypatch.setattr("automation.ats.detector.sync_playwright", _fail_if_called)

    result = detect_ats_for_url("https://boards.greenhouse.io/acme/jobs/123")
    assert result["ats"] == "greenhouse"
    assert result["method"] == "url_pattern"


def test_detect_ats_for_url_launches_browser_for_dom_fallback():
    data_url = "data:text/html,<html><body><div id='grnhse_app'></div></body></html>"
    try:
        result = detect_ats_for_url(data_url)
    except PlaywrightError as e:
        pytest.skip(f"Chromium not installed for Playwright — run `playwright install chromium`: {e}")
        return

    assert result["ats"] == "greenhouse"
    assert result["method"] == "dom_fingerprint"
