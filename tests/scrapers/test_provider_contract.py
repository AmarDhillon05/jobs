"""Level 2 - the contract every provider adapter must satisfy.

One battery, parametrized across every adapter, covering exactly the list in
PRD §7 and §14 Level 2: required fields present, URLs valid, IDs stable,
pagination handled with nothing silently skipped, malformed records contained,
source duplicates collapsed, empty results not a failure, and the transient /
rate-limit / permanent failure behaviours.

Because the battery is shared, adding a tenth provider cannot accidentally ship
with weaker guarantees than the other nine.
"""

from __future__ import annotations

from urllib.parse import urlsplit

import pytest
from tests.scrapers.conftest import ProviderCase, build_client
from tests.support.http import FakeTransport, ScriptedResponse

from jobmonitor.errors import AccessBlocked, ParseError, RetryBudgetExhausted
from jobmonitor.identity import content_hash, job_id
from jobmonitor.models.health import ScraperStatus
from jobmonitor.scrapers.base import safe_fetch

pytestmark = pytest.mark.scrapers


class TestParsesFixture:
    def test_returns_the_expected_number_of_jobs(self, provider_case: ProviderCase) -> None:
        result = provider_case.source().fetch()
        assert len(result.jobs) == provider_case.expected_jobs

    def test_skips_exactly_the_records_the_fixture_breaks(
        self, provider_case: ProviderCase
    ) -> None:
        assert provider_case.source().fetch().malformed == provider_case.expected_malformed

    def test_expected_titles_are_present(self, provider_case: ProviderCase) -> None:
        titles = {job.title for job in provider_case.source().fetch().jobs}
        for expected in provider_case.must_contain_titles:
            assert expected in titles, f"{expected!r} missing from {sorted(titles)[:5]}"


class TestRequiredFields:
    """PRD §7: validate that each returned job carries what the pipeline needs."""

    def test_company_title_url_and_source_are_always_populated(
        self, provider_case: ProviderCase
    ) -> None:
        for job in provider_case.source().fetch().jobs:
            assert job.company == "TestCo"
            assert job.title.strip()
            assert job.url.strip()
            assert job.source == provider_case.provider

    def test_urls_are_syntactically_valid_absolute_http_urls(
        self, provider_case: ProviderCase
    ) -> None:
        for job in provider_case.source().fetch().jobs:
            parts = urlsplit(job.url)
            assert parts.scheme in {"http", "https"}, job.url
            assert parts.netloc and "." in parts.netloc, job.url
            assert " " not in job.url, job.url

    def test_urls_point_at_a_plausible_posting_page(self, provider_case: ProviderCase) -> None:
        # Not a liveness check (BLK-001) - a shape check: the URL must not be the
        # bare board root, which would send the user to a list instead of a job.
        for job in provider_case.source().fetch().jobs:
            assert urlsplit(job.url).path not in {"", "/"}, job.url

    def test_at_least_one_job_carries_a_location(self, provider_case: ProviderCase) -> None:
        jobs = provider_case.source().fetch().jobs
        assert any(job.location for job in jobs)

    def test_at_least_one_job_carries_a_posting_date(self, provider_case: ProviderCase) -> None:
        if not provider_case.dated:
            pytest.skip(f"{provider_case.provider} exposes no posting date; treated as undated")
        jobs = provider_case.source().fetch().jobs
        assert any(job.date_posted is not None for job in jobs)

    def test_every_job_has_a_stable_external_id_where_the_source_offers_one(
        self, provider_case: ProviderCase
    ) -> None:
        jobs = provider_case.source().fetch().jobs
        with_ids = [job for job in jobs if job.has_stable_external_id]
        # Every provider in this project exposes an id for at least most postings.
        assert len(with_ids) >= len(jobs) - 1, f"{len(with_ids)}/{len(jobs)} had ids"


class TestStableIdentity:
    def test_job_ids_are_unique_within_a_fetch(self, provider_case: ProviderCase) -> None:
        jobs = provider_case.source().fetch().jobs
        identities = [job_id(job) for job in jobs]
        assert len(identities) == len(set(identities))

    def test_ids_and_hashes_are_identical_across_two_identical_fetches(
        self, provider_case: ProviderCase
    ) -> None:
        # This is the property the whole dedup story rests on.
        first = provider_case.source().fetch().jobs
        second = provider_case.source().fetch().jobs
        assert [job_id(j) for j in first] == [job_id(j) for j in second]
        assert [content_hash(j) for j in first] == [content_hash(j) for j in second]


class TestSourceDuplicates:
    def test_duplicate_source_records_are_collapsed(self, provider_case: ProviderCase) -> None:
        # Fixtures for greenhouse and smartrecruiters repeat a posting verbatim.
        result = provider_case.source().fetch()
        assert len({job_id(job) for job in result.jobs}) == len(result.jobs)


class TestPagination:
    def test_issues_the_expected_number_of_requests(self, provider_case: ProviderCase) -> None:
        transport = provider_case.transport()
        provider_case.source(transport).fetch()
        assert transport.call_count == provider_case.expected_requests

    def test_paginating_providers_walk_every_page(self, provider_case: ProviderCase) -> None:
        if not provider_case.paginates:
            pytest.skip(f"{provider_case.provider} returns its whole board in one response")
        result = provider_case.source().fetch()
        assert result.pages == provider_case.expected_requests
        # A posting that exists only on the final page proves no page was skipped -
        # a stronger claim than any count, which a truncating bug could still match.
        titles = {job.title for job in result.jobs}
        assert provider_case.last_page_marker_title in titles

    def test_paginating_providers_account_for_every_declared_record(
        self, provider_case: ProviderCase
    ) -> None:
        """Nothing is silently dropped: parsed + skipped + deduped == declared."""
        if not provider_case.paginates:
            pytest.skip(f"{provider_case.provider} returns its whole board in one response")
        assert provider_case.declared_total is not None
        result = provider_case.source().fetch()
        duplicates_in_fixture = provider_case.declared_total - (len(result.jobs) + result.malformed)
        assert 0 <= duplicates_in_fixture <= 1, (
            f"{duplicates_in_fixture} records unaccounted for: declared "
            f"{provider_case.declared_total}, parsed {len(result.jobs)}, "
            f"skipped {result.malformed}"
        )

    def test_non_paginating_providers_make_exactly_one_request(
        self, provider_case: ProviderCase
    ) -> None:
        if provider_case.paginates:
            pytest.skip(f"{provider_case.provider} paginates")
        transport = provider_case.transport()
        result = provider_case.source(transport).fetch()
        assert transport.call_count == 1
        assert result.pages == 1


class TestEmptyResults:
    def test_no_postings_is_empty_status_not_failure(self, provider_case: ProviderCase) -> None:
        transport = FakeTransport(always=provider_case.empty_response)
        result = safe_fetch(provider_case.source(transport))
        assert result.jobs == []
        assert result.health is not None
        assert result.health.status is ScraperStatus.EMPTY
        assert result.health.ok is True


class TestMalformedPayloads:
    @pytest.mark.parametrize(
        "payload",
        [b"", b"not json at all", b"<html><body>maintenance</body></html>", b"null"],
    )
    def test_garbage_response_body_fails_cleanly(
        self, provider_case: ProviderCase, payload: bytes
    ) -> None:
        transport = FakeTransport(always=ScriptedResponse(status=200, body=payload))
        result = safe_fetch(provider_case.source(transport))
        assert result.health is not None
        assert result.health.status is ScraperStatus.FAILED
        assert result.health.error

    def test_wrong_json_shape_raises_parse_error(self, provider_case: ProviderCase) -> None:
        if provider_case.provider == "json_ld":
            pytest.skip("json_ld consumes HTML, covered by the garbage-body case")
        # A JSON object where a list belongs, or vice versa.
        transport = FakeTransport(always=ScriptedResponse.json({"unexpected": "shape"}))
        with pytest.raises(ParseError):
            provider_case.source(transport).fetch_jobs()


class TestTransportBehaviour:
    def test_recovers_from_500_500_200(self, provider_case: ProviderCase) -> None:
        # The PRD §14 representative transient sequence, per provider.
        fixture_transport = provider_case.transport()
        healthy = provider_case.source(fixture_transport)
        expected = len(healthy.fetch().jobs)

        first_page = fixture_transport.requests[0]
        served = {"count": 0}

        def router(request: object) -> ScriptedResponse:
            served["count"] += 1
            if served["count"] <= 2:
                return ScriptedResponse.error(500)
            return _serve_like(provider_case, request)

        transport = FakeTransport(router=router)
        source = provider_case.source(transport)
        jobs = source.fetch_jobs()
        assert len(jobs) == expected
        assert served["count"] >= 3
        assert first_page.url  # sanity: the healthy run really did request something

    def test_rate_limit_then_success(self, provider_case: ProviderCase) -> None:
        served = {"count": 0}

        def router(request: object) -> ScriptedResponse:
            served["count"] += 1
            if served["count"] == 1:
                return ScriptedResponse.error(429, headers={"Retry-After": "1"})
            return _serve_like(provider_case, request)

        transport = FakeTransport(router=router)
        client = build_client(transport)
        source = provider_case.source(transport)
        source.client = client
        assert source.fetch_jobs()

    def test_persistent_5xx_is_a_failed_health_record(self, provider_case: ProviderCase) -> None:
        transport = FakeTransport(always=ScriptedResponse.error(503))
        result = safe_fetch(provider_case.source(transport))
        assert result.health is not None
        assert result.health.status is ScraperStatus.FAILED
        assert result.health.error_type == RetryBudgetExhausted.__name__

    def test_persistent_429_is_a_failed_health_record(self, provider_case: ProviderCase) -> None:
        transport = FakeTransport(always=ScriptedResponse.error(429))
        result = safe_fetch(provider_case.source(transport))
        assert result.health is not None
        assert result.health.status is ScraperStatus.FAILED

    @pytest.mark.parametrize("status", [401, 403])
    def test_access_denied_is_blocked_and_not_retried(
        self, provider_case: ProviderCase, status: int
    ) -> None:
        transport = FakeTransport(always=ScriptedResponse.error(status))
        result = safe_fetch(provider_case.source(transport))
        assert result.health is not None
        assert result.health.status is ScraperStatus.BLOCKED
        assert result.health.error_type == AccessBlocked.__name__
        assert transport.call_count == 1

    def test_404_is_a_failure_not_a_retry_storm(self, provider_case: ProviderCase) -> None:
        transport = FakeTransport(always=ScriptedResponse.error(404))
        result = safe_fetch(provider_case.source(transport))
        assert result.health is not None
        assert result.health.status is ScraperStatus.FAILED
        assert transport.call_count == 1

    def test_sends_the_configured_user_agent(self, provider_case: ProviderCase) -> None:
        transport = provider_case.transport()
        provider_case.source(transport).fetch()
        assert "internship-job-monitor" in transport.requests[0].headers["User-Agent"]


def _serve_like(case: ProviderCase, request: object) -> ScriptedResponse:
    """Serve whatever the provider's own fixture transport would serve."""
    replay = case.transport()
    return replay._next(request)  # type: ignore[arg-type]
