"""Opt-in live spot checks against real public ATS endpoints (PRD §31).

**Never a completion gate.** PRD §31 says *"Do not make the full test suite
dependent on every external careers site"* and §14 Level 7 says completion *"must
not depend on an external site remaining online"*. Accordingly every test here
carries the ``live`` marker, which ``make test`` and ``make verify`` exclude, and
each one skips unless ``ENABLE_LIVE_TESTS=1``.

In this sandbox they cannot run at all: the egress policy answers 403 to CONNECT
for every ATS host (BLOCKERS.md BLK-001). They are kept because they are the
check the user should run on a normal network:

    make test-live

What they answer (the §31 questions): does the endpoint respond, do we parse
current postings, are the application links well-formed, is pagination
accounted for, and are the IDs stable across two consecutive fetches?
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit

import pytest

from jobmonitor.config import Settings
from jobmonitor.http import HttpClient
from jobmonitor.identity import job_id
from jobmonitor.models.company import load_default_registry
from jobmonitor.scrapers import build_source

pytestmark = [pytest.mark.live, pytest.mark.scrapers]

#: One representative company per reusable ATS provider. Deliberately small:
#: spot checks, not a 150-company crawl.
SPOT_CHECKS = [
    ("greenhouse", "Anthropic"),
    ("lever", "Palantir"),
    ("ashby", "OpenAI"),
    ("smartrecruiters", "ServiceNow"),
    ("workday", "NVIDIA"),
]


def live_enabled() -> bool:
    return os.environ.get("ENABLE_LIVE_TESTS", "0").strip().lower() in {"1", "true", "yes"}


@pytest.fixture(autouse=True)
def _require_opt_in() -> None:
    if not live_enabled():
        pytest.skip("live tests are opt-in: set ENABLE_LIVE_TESTS=1 (see BLOCKERS.md BLK-001)")


@pytest.fixture(scope="module")
def client() -> HttpClient:
    # Real settings, real transport: this is the only suite that touches the network.
    return HttpClient(Settings.from_env().http)


@pytest.mark.parametrize(("provider", "company_name"), SPOT_CHECKS, ids=[c for _, c in SPOT_CHECKS])
class TestLiveProviderSpotCheck:
    def _source(self, provider: str, company_name: str, client: HttpClient):
        company = load_default_registry().get(company_name)
        assert company is not None, f"{company_name} is not in companies.json"
        assert company.provider == provider, (
            f"{company_name} now uses {company.provider}, not {provider}; update SPOT_CHECKS"
        )
        return build_source(company, client)

    def test_endpoint_responds_and_parses(
        self, provider: str, company_name: str, client: HttpClient
    ) -> None:
        result = self._source(provider, company_name, client).fetch()
        # Zero postings is a legitimate answer; an exception is not.
        assert result.malformed == 0 or result.jobs, (
            f"{company_name}: {result.malformed} unparseable records and no usable jobs"
        )

    def test_application_links_are_well_formed(
        self, provider: str, company_name: str, client: HttpClient
    ) -> None:
        jobs = self._source(provider, company_name, client).fetch_jobs()
        if not jobs:
            pytest.skip(f"{company_name} has no open postings right now")
        for job in jobs[:25]:
            parts = urlsplit(job.url)
            assert parts.scheme == "https", job.url
            assert parts.netloc, job.url
            assert parts.path not in {"", "/"}, job.url

    def test_ids_are_stable_across_two_consecutive_fetches(
        self, provider: str, company_name: str, client: HttpClient
    ) -> None:
        first = self._source(provider, company_name, client).fetch_jobs()
        if not first:
            pytest.skip(f"{company_name} has no open postings right now")
        second = self._source(provider, company_name, client).fetch_jobs()
        # Set comparison, not list: a board may legitimately reorder between calls.
        assert {job_id(j) for j in first} == {job_id(j) for j in second}

    def test_pagination_is_accounted_for(
        self, provider: str, company_name: str, client: HttpClient
    ) -> None:
        result = self._source(provider, company_name, client).fetch()
        if provider in {"smartrecruiters", "workday"} and len(result.jobs) >= 20:
            assert result.pages > 1, (
                f"{company_name} returned {len(result.jobs)} jobs in one page - "
                "pagination may have stopped early"
            )
        else:
            assert result.pages >= 1
