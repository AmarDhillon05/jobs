"""Provider adapters.

Importing this package registers every adapter, so ``build_source(company)``
works for any provider in ``companies.json`` without the caller knowing which
module implements it.

Reusable ATS adapters (PRD §6 - one adapter per provider, not one per company):

===================  ======================================  ==========
provider             endpoint                                registry
===================  ======================================  ==========
``greenhouse``       boards-api.greenhouse.io                52
``workday``          {tenant}.wdN.myworkdayjobs.com (CXS)    45
``ashby``            api.ashbyhq.com/posting-api             24
``lever``            api.lever.co/v0/postings                13
``smartrecruiters``  api.smartrecruiters.com/v1              5
``rippling``         api.rippling.com/.../ats/v1/board       1
``workable``         apply.workable.com/api/v1/widget        0 (ready)
===================  ======================================  ==========

Custom scrapers:

===================  =====================================================
``json_ld``          schema.org JobPosting embedded in a careers page
``simplify_fallback``  the Simplify community feed, as a *secondary*
                     fallback only, for the 11 companies whose own
                     endpoint could not be configured (PRD §4.3)
===================  =====================================================
"""

from jobmonitor.scrapers.ashby import AshbySource
from jobmonitor.scrapers.base import (
    FetchResult,
    JobSource,
    build_source,
    build_sources,
    register,
    registered_providers,
    safe_fetch,
    source_class_for,
)
from jobmonitor.scrapers.custom.json_ld import JsonLdSource
from jobmonitor.scrapers.custom.simplify_fallback import SimplifyFallbackSource
from jobmonitor.scrapers.greenhouse import GreenhouseSource
from jobmonitor.scrapers.lever import LeverSource
from jobmonitor.scrapers.rippling import RipplingSource
from jobmonitor.scrapers.smartrecruiters import SmartRecruitersSource
from jobmonitor.scrapers.workable import WorkableSource
from jobmonitor.scrapers.workday import WorkdaySource

#: Reusable ATS provider adapters.
ATS_PROVIDERS: tuple[str, ...] = (
    "greenhouse",
    "lever",
    "ashby",
    "workday",
    "smartrecruiters",
    "workable",
    "rippling",
)
#: Company/site-specific adapters.
CUSTOM_PROVIDERS: tuple[str, ...] = ("json_ld", "simplify_fallback")

__all__ = [
    "ATS_PROVIDERS",
    "CUSTOM_PROVIDERS",
    "AshbySource",
    "FetchResult",
    "GreenhouseSource",
    "JobSource",
    "JsonLdSource",
    "LeverSource",
    "RipplingSource",
    "SimplifyFallbackSource",
    "SmartRecruitersSource",
    "WorkableSource",
    "WorkdaySource",
    "build_source",
    "build_sources",
    "register",
    "registered_providers",
    "safe_fetch",
    "source_class_for",
]
