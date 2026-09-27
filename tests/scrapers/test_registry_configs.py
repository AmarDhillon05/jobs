"""Level 2 - every company in companies.json, against its own adapter.

This file is the automated evidence behind the word "supported" in
``companies.json`` (PRD §32: *"Do not claim a company is supported unless its
configured source passes the appropriate scraper test."*).

For **each of the 150 registry entries** it proves:

1. the adapter can be constructed from that company's ``provider_config`` -
   i.e. no missing or blank required key;
2. the request the adapter would issue is a well-formed absolute HTTPS URL
   against that provider's real API host;
3. replaying that provider's saved fixture through *this company's* adapter
   yields valid jobs attributed to this company.

What it does **not** prove is that the slug is still live - the sandbox's egress
policy blocks every ATS host (BLOCKERS.md BLK-001). ``make validate-companies``
closes that last gap outside the sandbox. The precise definition of "supported"
is recorded in COMPANY_COVERAGE.md.
"""

from __future__ import annotations

import copy
from urllib.parse import urlsplit

import pytest
from tests.scrapers.conftest import CASES_BY_PROVIDER, build_client
from tests.support.http import FakeTransport, ScriptedResponse, load_fixture

from jobmonitor.models.company import Company, SupportStatus, load_default_registry
from jobmonitor.scrapers import build_source
from jobmonitor.scrapers.base import JobSource

pytestmark = pytest.mark.scrapers

REGISTRY = load_default_registry()
POLLABLE = REGISTRY.pollable()

#: The API host each provider's requests must be aimed at.
EXPECTED_HOSTS = {
    "greenhouse": "boards-api.greenhouse.io",
    "lever": "api.lever.co",
    "ashby": "api.ashbyhq.com",
    "smartrecruiters": "api.smartrecruiters.com",
    "workable": "apply.workable.com",
    "rippling": "api.rippling.com",
    "simplify_fallback": "raw.githubusercontent.com",
    "amazon": "www.amazon.jobs",
    "google": "www.google.com",
    "goldman": "api-higher.gs.com",
    "ibm": "www-api.ibm.com",
    "atlassian": "www.atlassian.com",
}

#: Providers whose one fixed endpoint serves exactly one company, so there is no
#: per-company identifier to find in the URL - the host check above is the check.
SINGLE_COMPANY_PROVIDERS = frozenset({"amazon", "google", "goldman", "ibm", "atlassian"})

#: Providers whose tenant *is* the host (the host comes from the company's own
#: config), so identity is proven by the request going to that host.
HOST_IS_IDENTITY_PROVIDERS = frozenset({"eightfold", "oracle_hcm", "jibe", "talentbrew"})

#: Config keys that tune *how* a board is read, never *which* board it is.
NON_IDENTIFYING_KEYS = frozenset(
    {
        "host",
        "tenant",
        "locale",
        "search_text",
        "base_url",
        "queries",
        "query",
        "keyword",
        "keywords",
        "job_path",
        "api",
        "region",
        "experiences",
        "language",
    }
)


def request_url(source: JobSource) -> str:
    """The URL this adapter would fetch first, without fetching it."""
    for attribute in ("jobs_url", "feed_url", "page_url"):
        value = getattr(source, attribute, None)
        if callable(value):
            value = value(0)
        if isinstance(value, str):
            return value
    raise AssertionError(f"{type(source).__name__} exposes no request URL to inspect")


def ids(companies: tuple[Company, ...]) -> list[str]:
    return [company.company for company in companies]


def fallback_transport_for(company: Company) -> FakeTransport:
    """The Simplify fixture, re-attributed to ``company``'s configured names.

    The fallback adapter selects rows by employer name, so replaying the shared
    feed against a company that is not in it would prove nothing. Re-attributing
    the rows keeps the payload *shape* real while making the filter meaningful.
    """
    names = [str(name) for name in company.provider_config.get("company_names", [])]
    assert names, company.company
    rows = copy.deepcopy(load_fixture("simplify", "listings.json"))
    for index, row in enumerate(rows):
        if isinstance(row, dict) and row.get("company_name") not in (None, "SomeOtherCompany"):
            row["company_name"] = names[index % len(names)]
    return FakeTransport([ScriptedResponse.json(rows)])


@pytest.mark.parametrize("company", POLLABLE, ids=ids(POLLABLE))
class TestEveryPollableCompany:
    def test_adapter_constructs_from_its_registry_config(self, company: Company) -> None:
        source = build_source(company)
        assert source.provider == company.provider

    def test_request_url_is_absolute_https_against_the_expected_api_host(
        self, company: Company
    ) -> None:
        url = request_url(build_source(company))
        parts = urlsplit(url)
        assert parts.scheme == "https", url
        assert parts.netloc, url
        assert " " not in url, url
        expected = EXPECTED_HOSTS.get(company.provider)
        if company.provider == "lever" and company.provider_config.get("region") == "eu":
            expected = "api.eu.lever.co"  # Lever's separate EU instance
        if expected:
            assert parts.netloc == expected, url
        else:
            # Workday and json_ld are per-tenant/per-site hosts.
            assert "." in parts.netloc, url

    def test_request_url_embeds_this_company_s_own_configuration(self, company: Company) -> None:
        """Guards against a copy-paste that points two companies at one board."""
        url = request_url(build_source(company))
        if company.provider == "simplify_fallback":
            pytest.skip("the fallback feed URL is shared by design; filtering is by name")
        if company.provider in SINGLE_COMPANY_PROVIDERS:
            pytest.skip(
                f"{company.provider}'s endpoint serves only this company; host checked above"
            )
        if company.provider in HOST_IS_IDENTITY_PROVIDERS:
            host = str(company.provider_config["host"])
            assert urlsplit(url).netloc == host, f"{company.company}: {url}"
            if company.provider == "eightfold":
                assert f"domain={company.provider_config['domain']}" in url, url
            if company.provider == "oracle_hcm":
                assert f"siteNumber={company.provider_config['site']}" in url, url
            return
        identifying: list[str] = []
        for key, value in company.provider_config.items():
            if key in NON_IDENTIFYING_KEYS:
                continue
            # A candidate list identifies the company through any one of its
            # entries; the adapter builds its first request from the first.
            if isinstance(value, list):
                identifying.extend(str(item) for item in value)
            else:
                identifying.append(str(value))
        assert identifying, company.company
        assert any(value in url for value in identifying), f"{company.company}: {url}"

    def test_replaying_the_provider_fixture_produces_jobs_for_this_company(
        self, company: Company
    ) -> None:
        case = CASES_BY_PROVIDER[company.provider]
        if company.provider == "simplify_fallback":
            # The shared feed is filtered *by employer name*, so this company's
            # rows have to exist in it for the replay to mean anything.
            transport = fallback_transport_for(company)
        else:
            transport = case.transport()
        source = build_source(company, build_client(transport))
        jobs = source.fetch_jobs()
        assert jobs, company.company
        assert all(job.company == company.company for job in jobs)
        assert all(job.source == company.provider for job in jobs)
        assert all(job.title and job.url for job in jobs)

    def test_careers_url_is_a_usable_absolute_link(self, company: Company) -> None:
        parts = urlsplit(company.careers_url)
        assert parts.scheme in {"http", "https"}, company.careers_url
        assert "." in parts.netloc, company.careers_url


class TestRegistryWideInvariants:
    def test_every_pollable_provider_has_a_fixture_backed_test_case(self) -> None:
        missing = {c.provider for c in POLLABLE} - set(CASES_BY_PROVIDER)
        assert not missing, f"providers without a fixture case: {sorted(missing)}"

    def test_supported_entries_use_the_company_s_own_ats_not_the_fallback_feed(self) -> None:
        for company in REGISTRY.with_status(SupportStatus.SUPPORTED):
            assert company.provider != "simplify_fallback", (
                f"{company.company} is marked supported but reads a community feed; "
                "that is `partial` by definition"
            )

    def test_partial_entries_explain_why_they_are_only_partial(self) -> None:
        """Two legitimate reasons to be partial, and each must say which."""
        partial = REGISTRY.with_status(SupportStatus.PARTIAL)
        assert partial
        for company in partial:
            notes = company.notes.casefold()
            reason_given = (
                # (a) read through the community feed rather than the employer;
                "fallback" in notes
                # (b) the employer's own board, with the token resolved at runtime.
                or "candidate" in notes
            )
            assert reason_given, f"{company.company}: {company.notes!r}"

    def test_runtime_resolved_entries_carry_their_evidence(self) -> None:
        """A candidate config must say what proved the provider (PRD §35)."""
        for company in REGISTRY.with_status(SupportStatus.PARTIAL):
            if "board_token_candidates" not in company.provider_config:
                continue
            notes = company.notes.casefold()
            assert "evidence" in notes, company.company
            assert "gh_jid" in notes or "embed" in notes, company.company
            assert len(company.provider_config["board_token_candidates"]) >= 2, company.company

    def test_fallback_feed_is_a_small_minority_of_the_registry(self) -> None:
        # PRD §4.3: the seed repos are a guide, not the live detection mechanism.
        fallback = len(REGISTRY.by_provider("simplify_fallback"))
        assert fallback / len(POLLABLE) < 0.15, f"{fallback}/{len(POLLABLE)} on the fallback feed"

    def test_no_real_company_uses_the_test_only_fixture_provider(self) -> None:
        """`fixture` is a deterministic stand-in for the architecture test only."""
        from jobmonitor.scrapers import TEST_PROVIDERS

        offenders = [c.company for c in REGISTRY if c.provider in TEST_PROVIDERS]
        assert not offenders, f"registry entries using a test provider: {offenders}"

    def test_no_two_companies_share_a_fallback_feed_company_name(self) -> None:
        claimed: dict[str, str] = {}
        for company in REGISTRY.by_provider("simplify_fallback"):
            for name in company.provider_config.get("company_names", []):
                key = str(name).casefold()
                assert key not in claimed, f"{company.company} and {claimed[key]} both claim {name}"
                claimed[key] = company.company
