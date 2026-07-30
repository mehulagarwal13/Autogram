"""
automation/ats/registry.py — maps a detected ATS platform name to its real
ATSAdapter class, or `None` for platforms that are still Phase 7 stubs.
"""

from automation.ats.greenhouse.greenhouse_adapter import GreenhouseAdapter
from automation.ats.lever.lever_adapter import LeverAdapter
from automation.ats.registry import get_adapter_class, is_supported


def test_get_adapter_class_returns_the_real_adapter_for_supported_platforms():
    assert get_adapter_class("greenhouse") is GreenhouseAdapter
    assert get_adapter_class("lever") is LeverAdapter


def test_get_adapter_class_returns_none_for_still_stub_platforms():
    assert get_adapter_class("workday") is None
    assert get_adapter_class("smartrecruiters") is None


def test_get_adapter_class_returns_none_for_the_generic_fallback():
    assert get_adapter_class("custom") is None


def test_is_supported_matches_get_adapter_class():
    assert is_supported("greenhouse") is True
    assert is_supported("lever") is True
    assert is_supported("workday") is False
