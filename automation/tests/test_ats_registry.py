"""
automation/ats/registry.py — maps a detected ATS platform name to its real
ATSAdapter class, or `None` for platforms that are still Phase 7 stubs.
"""

from automation.ats.greenhouse.greenhouse_adapter import GreenhouseAdapter
from automation.ats.lever.lever_adapter import LeverAdapter
from automation.ats.registry import get_adapter_class, is_supported
from automation.ats.workday.workday_adapter import WorkdayAdapter


def test_get_adapter_class_returns_the_real_adapter_for_supported_platforms():
    assert get_adapter_class("greenhouse") is GreenhouseAdapter
    assert get_adapter_class("lever") is LeverAdapter


def test_workday_is_registered_now_that_multi_page_applications_work():
    # Registered only once the flow manager could actually finish a 4-6 page
    # application. Routing to it earlier would have filled page 1 and reported
    # a confident result for a form that was five-sixths unanswered — worse
    # than the honest `needs_review` an unregistered platform produces.
    assert get_adapter_class("workday") is WorkdayAdapter
    assert is_supported("workday") is True


def test_get_adapter_class_returns_none_for_still_stub_platforms():
    assert get_adapter_class("smartrecruiters") is None
    assert get_adapter_class("taleo") is None


def test_get_adapter_class_returns_none_for_the_generic_fallback():
    assert get_adapter_class("custom") is None


def test_is_supported_matches_get_adapter_class():
    assert is_supported("greenhouse") is True
    assert is_supported("lever") is True
    assert is_supported("smartrecruiters") is False
