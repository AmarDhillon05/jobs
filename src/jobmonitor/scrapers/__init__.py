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
``eightfold``        {tenant}/api/pcsx/search (Eightfold AI)  4
``oracle_hcm``       {tenant}.fa.oraclecloud.com (Fusion HCM) 3
``jibe``             {site}/api/jobs (iCIMS Jibe)            2
``talentbrew``       {site}/search-jobs/results (Radancy)     1
``avature``          {site}/{portal}/sitemap.xml (Avature)    1
===================  ======================================  ==========

Custom scrapers:

===================  =====================================================
``amazon``           amazon.jobs search.json
``google``           google.com/about/careers (data embedded in the page)
``goldman``          higher.gs.com GraphQL (campus roles)
``ibm``              IBM careers search API (no posting dates)
``atlassian``        atlassian.com careers listings
``json_ld``          schema.org JobPosting embedded in a careers page
``simplify_fallback``  the Simplify community feed, as a *secondary*
                     fallback only, for the 11 companies whose own
                     endpoint could not be configured (PRD §4.3)
===================  =====================================================
"""

from jobmonitor.scrapers.amazon import AmazonJobsSource
from jobmonitor.scrapers.ashby import AshbySource
from jobmonitor.scrapers.atlassian import AtlassianSource
from jobmonitor.scrapers.avature import AvatureSource
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
from jobmonitor.scrapers.custom.fixture import FixtureSource
from jobmonitor.scrapers.custom.json_ld import JsonLdSource
from jobmonitor.scrapers.custom.simplify_fallback import SimplifyFallbackSource
from jobmonitor.scrapers.eightfold import EightfoldSource
from jobmonitor.scrapers.goldman import GoldmanSachsSource
from jobmonitor.scrapers.google import GoogleCareersSource
from jobmonitor.scrapers.greenhouse import GreenhouseSource
from jobmonitor.scrapers.ibm import IbmCareersSource
from jobmonitor.scrapers.jibe import JibeSource
from jobmonitor.scrapers.lever import LeverSource
from jobmonitor.scrapers.oracle_hcm import OracleHcmSource
from jobmonitor.scrapers.rippling import RipplingSource
from jobmonitor.scrapers.smartrecruiters import SmartRecruitersSource
from jobmonitor.scrapers.talentbrew import TalentBrewSource
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
    "eightfold",
    "oracle_hcm",
    "jibe",
    "talentbrew",
    "avature",
)
#: Company/site-specific adapters.
CUSTOM_PROVIDERS: tuple[str, ...] = (
    "amazon",
    "goldman",
    "google",
    "ibm",
    "atlassian",
    "json_ld",
    "simplify_fallback",
)
#: Not a real source: a deterministic stand-in used only by the local
#: architecture test. No companies.json entry uses it (enforced by a test).
TEST_PROVIDERS: tuple[str, ...] = ("fixture",)

__all__ = [
    "ATS_PROVIDERS",
    "CUSTOM_PROVIDERS",
    "TEST_PROVIDERS",
    "AmazonJobsSource",
    "AshbySource",
    "AtlassianSource",
    "AvatureSource",
    "EightfoldSource",
    "FetchResult",
    "FixtureSource",
    "GoldmanSachsSource",
    "GoogleCareersSource",
    "GreenhouseSource",
    "IbmCareersSource",
    "JibeSource",
    "JobSource",
    "JsonLdSource",
    "LeverSource",
    "OracleHcmSource",
    "RipplingSource",
    "SimplifyFallbackSource",
    "SmartRecruitersSource",
    "TalentBrewSource",
    "WorkableSource",
    "WorkdaySource",
    "build_source",
    "build_sources",
    "register",
    "registered_providers",
    "safe_fetch",
    "source_class_for",
]
