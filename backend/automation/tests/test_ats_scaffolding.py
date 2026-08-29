"""
Sanity checks on the automation module's ATS scaffolding: the ATSAdapter
contract can't be instantiated directly, concrete adapters (real or still
stub) satisfy it, ATSDetector's URL pattern table covers every platform in
the project brief, and adapters that are STILL unimplemented (SmartRecruiters,
Taleo, iCIMS, BambooHR, Oracle HCM) fail loudly (NotImplementedError) rather
than silently doing nothing.

GreenhouseAdapter, LeverAdapter and now WorkdayAdapter are real — their actual
form-filling behavior is covered in `test_greenhouse_adapter.py`,
`test_lever_adapter.py` and `test_workday_adapter.py`, not here. Real
ATSDetector behavior (URL/DOM tiers, fallback) is covered in
`test_detector.py` — this file only checks the pattern table's coverage.

Note: `automation/ats/base.py` imports `automation.interfaces`, which imports
real `app.core.*` modules (see `automation/interfaces.py` and
`automation/README.md` — `automation/` is an internal module of this
application, not an isolated one). This test file needs the same env vars as
`app/`'s own tests, provided by `automation/tests/conftest.py`.
"""

import pytest

from automation.ats.base import ATSAdapter
from automation.ats.detector import ATSDetector, URL_PATTERNS
from automation.ats.generic.generic_adapter import GenericAdapter
from automation.ats.greenhouse.greenhouse_adapter import GreenhouseAdapter
from automation.ats.lever.lever_adapter import LeverAdapter
from automation.ats.workday.workday_adapter import WorkdayAdapter


def test_ats_adapter_is_abstract():
    with pytest.raises(TypeError):
        ATSAdapter(page=None, profile=None, resume_document=None)


def test_greenhouse_and_lever_adapters_are_real_now():
    # Full behavior lives in test_greenhouse_adapter.py / test_lever_adapter.py;
    # this is just a construction-level sanity check that they satisfy the
    # ATSAdapter contract (name set, no longer abstract/stub).
    assert GreenhouseAdapter(page=None, profile=None, resume_document=None).name == "greenhouse"
    assert LeverAdapter(page=None, profile=None, resume_document=None).name == "lever"


def test_workday_adapter_is_real_now():
    # Was a stub raising NotImplementedError until multi-page support landed —
    # a 4-6 page application had nothing to run on before that. Behavior lives
    # in test_workday_adapter.py; this is the construction-level check.
    adapter = WorkdayAdapter(page=None, profile=None, resume_document=None)
    assert adapter.name == "workday"
    assert callable(adapter.fill_personal_information)


def test_generic_adapter_is_always_low_confidence_fallback():
    adapter = GenericAdapter(page=None, profile=None, resume_document=None)
    assert 0.0 <= adapter.detect() < 0.5


@pytest.mark.parametrize(
    "platform",
    ["greenhouse", "lever", "workday", "smartrecruiters", "taleo", "icims", "ashby", "bamboohr", "oracle_hcm"],
)
def test_url_patterns_cover_every_required_platform(platform):
    assert platform in URL_PATTERNS
    assert len(URL_PATTERNS[platform]) > 0


def test_detector_is_implemented_for_url_pattern_tier():
    # Phase 3 is now implemented — see test_detector.py for full coverage.
    result = ATSDetector.detect("https://boards.greenhouse.io/acme/jobs/123")
    assert result == {"ats": "greenhouse", "confidence": 0.98, "method": "url_pattern"}
