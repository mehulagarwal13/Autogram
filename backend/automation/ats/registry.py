"""
ADAPTER_REGISTRY — maps an `ATSDetector` platform name (see `automation/ats/detector.py`'s
`URL_PATTERNS`/`DOM_FINGERPRINTS`) to its concrete, REAL `ATSAdapter` class.

Only platforms with a real (non-stub) adapter belong here. The still-stub
subclasses (SmartRecruiters, Taleo, iCIMS, Ashby, BambooHR, Oracle HCM — Phase
7) are deliberately NOT registered: calling one would raise
`NotImplementedError` mid-run. Callers (`app/api/applications.py`) check
`get_adapter_class(...) is None` and hand the run to
`ApplicationFlowManager` anyway, which falls back to `GenericAdapter` (see
`automation/ats/generic/generic_adapter.py`) rather than an immediate
`needs_review` — automation is attempted with the generic label/name/
placeholder fill every real adapter also uses, and a human reviews the
result. `GenericAdapter` is a real, working fallback, not a crash path, but
it is still never a dedicated adapter for any of these platforms: see
`ApplicationFlowManager._fall_back_to_generic_adapter`, which forces
`ats_platform` to `"custom"` for exactly this reason — some of these platform
names (`"smartrecruiters"`, `"ashby"`) are members of
`ApplicationFlowManager.PUBLIC_ATS_PLATFORMS`, and without that reassignment
a confidently-detected-but-unregistered posting on one of them could
otherwise satisfy the AUTO_SUBMIT gate despite no dedicated adapter having
run.

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
