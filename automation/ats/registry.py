"""
ADAPTER_REGISTRY — maps an `ATSDetector` platform name (see `automation/ats/detector.py`'s
`URL_PATTERNS`/`DOM_FINGERPRINTS`) to its concrete, REAL `ATSAdapter` class.

Only platforms with a real (non-stub) adapter belong here. The still-stub
subclasses (SmartRecruiters, Taleo, iCIMS, Ashby, BambooHR, Oracle HCM — Phase
7) are deliberately NOT registered: calling one would raise
`NotImplementedError` mid-run. Callers (`app/api/applications.py`) check
`get_adapter_class(...) is None` and route straight to `needs_review` instead,
which is a real, honest, plannable outcome rather than a crash.

Workday joined this table once multi-page support landed. It could not usefully
have been registered before: a Workday application is 4-6 pages, and a flow
manager that filled page 1 and then treated it as the whole form would have
produced a confidently wrong result rather than a crash — the worse of the two.
"""

from __future__ import annotations

from automation.ats.base import ATSAdapter
from automation.ats.greenhouse.greenhouse_adapter import GreenhouseAdapter
from automation.ats.lever.lever_adapter import LeverAdapter
from automation.ats.workday.workday_adapter import WorkdayAdapter

ADAPTER_REGISTRY: dict[str, type[ATSAdapter]] = {
    "greenhouse": GreenhouseAdapter,
    "lever": LeverAdapter,
    "workday": WorkdayAdapter,
}


def get_adapter_class(ats_platform: str) -> type[ATSAdapter] | None:
    """The real adapter class for `ats_platform`, or `None` if automation for
    that platform isn't implemented yet."""
    return ADAPTER_REGISTRY.get(ats_platform)


def is_supported(ats_platform: str) -> bool:
    return ats_platform in ADAPTER_REGISTRY
