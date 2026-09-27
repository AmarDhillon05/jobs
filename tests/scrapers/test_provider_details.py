"""Level 2 - the quirks that are specific to one provider.

The shared battery in ``test_provider_contract.py`` proves every adapter behaves
the same way. This file proves each adapter reads *its own* payload correctly -
Greenhouse's double-escaped HTML, Lever's millisecond timestamps, Workday's
relative posting dates, and so on.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from tests.scrapers.conftest import case_for
from tests.support.http import FakeTransport, ScriptedResponse, load_fixture

from jobmonitor.errors import ParseError
from jobmonitor.identity import job_id
from jobmonitor.scrapers.custom.json_ld import extract_job_postings
from jobmonitor.scrapers.smartrecruiters import PAGE_SIZE as SR_PAGE_SIZE
from jobmonitor.scrapers.workday import PAGE_SIZE as WD_PAGE_SIZE
from jobmonitor.scrapers.workday import parse_posted_on

pytestmark = pytest.mark.scrapers


def jobs_for(provider: str) -> list:
    return case_for(provider).source().fetch().jobs


def by_title(provider: str, title: str):
    for job in jobs_for(provider):
        if job.title == title:
            return job
    raise AssertionError(f"{provider}: no job titled {title!r}")


class TestGreenhouse:
    def test_requests_the_documented_endpoint_with_content(self) -> None:
        case = case_for("greenhouse")
        transport = case.transport()
        case.source(transport).fetch()
        assert transport.urls == [
            "https://boards-api.greenhouse.io/v1/boards/testco/jobs?content=true"
        ]

    def test_double_escaped_html_is_decoded_then_stripped(self) -> None:
        job = by_title("greenhouse", "Software Engineer Intern, Summer 2027")
        assert job.description is not None
        # Neither raw entities nor tags may reach the user's email.
        assert "&lt;" not in job.description
        assert "<p>" not in job.description
        assert "Join our infrastructure team." in job.description
        assert "Python" in job.description

    def test_first_published_is_preferred_over_updated_at(self) -> None:
        # updated_at moves on every recruiter edit; using it would make old jobs
        # look newly posted.
        job = by_title("greenhouse", "Software Engineer Intern, Summer 2027")
        assert job.date_posted is not None
        assert job.date_posted.date().isoformat() == "2026-09-18"

    def test_falls_back_to_updated_at_when_first_published_is_absent(self) -> None:
        payload = {
            "jobs": [
                {
                    "id": 1,
                    "title": "Intern",
                    "absolute_url": "https://job-boards.greenhouse.io/testco/jobs/1",
                    "updated_at": "2026-05-05T00:00:00Z",
                }
            ]
        }
        case = case_for("greenhouse")
        jobs = case.source(FakeTransport([ScriptedResponse.json(payload)])).fetch_jobs()
        assert jobs[0].date_posted is not None
        assert jobs[0].date_posted.date().isoformat() == "2026-05-05"

    def test_offices_are_merged_into_the_location(self) -> None:
        job = by_title("greenhouse", "Machine Learning Engineer Intern")
        assert job.location is not None
        assert "New York, NY" in job.location
        assert "Remote" in job.location

    def test_employment_type_comes_from_board_metadata(self) -> None:
        assert by_title("greenhouse", "Software Engineer Intern, Summer 2027").employment_type == (
            "Intern"
        )

    def test_external_id_is_the_greenhouse_job_id(self) -> None:
        assert by_title("greenhouse", "Software Engineer Intern, Summer 2027").external_id == (
            "4020160008"
        )

    def test_declared_total_higher_than_returned_is_refused(self) -> None:
        """A partial board must never be reported as "all current postings"."""
        payload = {"jobs": [], "meta": {"total": 40}}
        case = case_for("greenhouse")
        with pytest.raises(ParseError, match="refusing to treat a partial board"):
            case.source(FakeTransport([ScriptedResponse.json(payload)])).fetch_jobs()

    def test_missing_jobs_key_is_a_parse_error(self) -> None:
        case = case_for("greenhouse")
        with pytest.raises(ParseError, match="no 'jobs' key"):
            case.source(FakeTransport([ScriptedResponse.json({"meta": {}})])).fetch_jobs()


class TestGreenhouseCandidateResolution:
    """Companies running a Greenhouse board behind their own domain.

    Their posting URLs carry `gh_jid`, which proves the provider but never the
    board token, so the token is resolved against the live API instead of being
    guessed and asserted. These tests pin the resolution rules.
    """

    def _company(self, *candidates: str):
        from tests.conftest import make_company

        return make_company(
            "Stripe", provider="greenhouse", config={"board_token_candidates": list(candidates)}
        )

    def _source(self, transport, *candidates: str):
        from tests.scrapers.conftest import build_client

        from jobmonitor.scrapers import build_source

        return build_source(self._company(*candidates), build_client(transport))

    def test_a_single_configured_token_makes_exactly_one_request(self) -> None:
        """The 139 evidence-derived companies must not pay for this feature."""
        case = case_for("greenhouse")
        transport = case.transport()
        case.source(transport).fetch()
        assert transport.call_count == 1

    def test_the_first_responding_candidate_wins(self) -> None:
        board = load_fixture("greenhouse", "board.json")

        def router(request):
            if "/boards/wrongtoken/" in request.url:
                return ScriptedResponse.error(404)
            return ScriptedResponse.json(board)

        transport = FakeTransport(router=router)
        source = self._source(transport, "wrongtoken", "stripe")
        jobs = source.fetch_jobs()
        assert jobs
        assert source.board_token == "stripe"
        assert "/boards/stripe/jobs" in transport.urls[-1]

    def test_the_winning_response_is_not_fetched_twice(self) -> None:
        board = load_fixture("greenhouse", "board.json")

        def router(request):
            return (
                ScriptedResponse.error(404)
                if "/boards/nope/" in request.url
                else ScriptedResponse.json(board)
            )

        transport = FakeTransport(router=router)
        self._source(transport, "nope", "stripe").fetch_jobs()
        # One probe that 404s, one that succeeds - and the success is reused.
        assert transport.call_count == 2

    def test_resolution_happens_once_per_instance(self) -> None:
        board = load_fixture("greenhouse", "board.json")
        transport = FakeTransport(always=ScriptedResponse.json(board))
        source = self._source(transport, "stripe", "stripejobs")
        source.fetch_jobs()
        source.fetch_jobs()
        assert transport.call_count == 2  # not 4: the token is remembered

    def test_all_candidates_missing_is_an_actionable_parse_error(self) -> None:
        transport = FakeTransport(always=ScriptedResponse.error(404))
        with pytest.raises(ParseError, match="none of the candidate Greenhouse boards"):
            self._source(transport, "one", "two").fetch_jobs()

    def test_the_error_names_the_candidates_it_tried(self) -> None:
        transport = FakeTransport(always=ScriptedResponse.error(404))
        with pytest.raises(ParseError) as excinfo:
            self._source(transport, "alpha", "beta").fetch_jobs()
        assert "alpha=404" in str(excinfo.value)
        assert "beta=404" in str(excinfo.value)
        assert "validate-companies" in str(excinfo.value)

    def test_a_403_is_not_mistaken_for_a_wrong_candidate(self) -> None:
        """A board that exists but blocks us is blocked, not misconfigured."""
        from jobmonitor.errors import AccessBlocked

        transport = FakeTransport(always=ScriptedResponse.error(403))
        with pytest.raises(AccessBlocked):
            self._source(transport, "alpha", "beta").fetch_jobs()
        assert transport.call_count == 1  # stopped at the first, did not keep probing

    def test_a_5xx_is_not_mistaken_for_a_wrong_candidate(self) -> None:
        from jobmonitor.errors import RetryBudgetExhausted

        transport = FakeTransport(always=ScriptedResponse.error(503))
        with pytest.raises(RetryBudgetExhausted):
            self._source(transport, "alpha", "beta").fetch_jobs()

    def test_an_empty_config_is_rejected_at_construction(self) -> None:
        from tests.conftest import make_company

        from jobmonitor.errors import ProviderConfigError
        from jobmonitor.scrapers import build_source

        with pytest.raises(ProviderConfigError, match="board_token_candidates"):
            build_source(make_company(provider="greenhouse", config={}))

    def test_jobs_are_attributed_to_the_company_not_the_board(self) -> None:
        board = load_fixture("greenhouse", "board.json")
        transport = FakeTransport(always=ScriptedResponse.json(board))
        jobs = self._source(transport, "stripe").fetch_jobs()
        assert all(job.company == "Stripe" for job in jobs)


class TestLever:
    def test_requests_the_documented_endpoint(self) -> None:
        case = case_for("lever")
        transport = case.transport()
        case.source(transport).fetch()
        assert transport.urls == ["https://api.lever.co/v0/postings/testco?mode=json"]

    def test_millisecond_created_at_is_interpreted_correctly(self) -> None:
        job = by_title("lever", "Software Engineer Intern (Summer 2027)")
        assert job.date_posted == datetime.fromtimestamp(1769728646, tz=UTC)

    def test_list_blocks_are_stitched_into_the_description(self) -> None:
        job = by_title("lever", "Software Engineer Intern (Summer 2027)")
        assert job.description is not None
        assert "Work on the platform team." in job.description
        assert "Requirements:" in job.description
        assert "Kubernetes" in job.description

    def test_all_locations_are_captured(self) -> None:
        job = by_title("lever", "Software Engineer Intern (Summer 2027)")
        assert job.location is not None
        assert "San Francisco" in job.location
        assert "New York" in job.location

    def test_remote_workplace_type_is_reflected_in_the_location(self) -> None:
        job = by_title("lever", "Site Reliability Engineer Intern")
        assert job.location is not None
        assert "Remote" in job.location

    def test_commitment_becomes_employment_type(self) -> None:
        assert by_title("lever", "Site Reliability Engineer Intern").employment_type == "Intern"

    def test_object_wrapped_response_is_accepted(self) -> None:
        # Some tenants wrap the array; failing on that would lose a whole company.
        payload = {
            "data": [
                {
                    "id": "abc",
                    "text": "Intern",
                    "hostedUrl": "https://jobs.lever.co/testco/abc",
                    "categories": {},
                }
            ]
        }
        case = case_for("lever")
        jobs = case.source(FakeTransport([ScriptedResponse.json(payload)])).fetch_jobs()
        assert len(jobs) == 1

    def test_object_without_a_postings_array_is_a_parse_error(self) -> None:
        case = case_for("lever")
        with pytest.raises(ParseError, match="no postings array"):
            case.source(FakeTransport([ScriptedResponse.json({"x": 1})])).fetch_jobs()


class TestAshby:
    def test_requests_the_documented_endpoint(self) -> None:
        case = case_for("ashby")
        transport = case.transport()
        case.source(transport).fetch()
        assert transport.urls == [
            "https://api.ashbyhq.com/posting-api/job-board/testco?includeCompensation=true"
        ]

    def test_unlisted_postings_are_skipped(self) -> None:
        titles = {job.title for job in jobs_for("ashby")}
        assert "Unlisted Draft Intern Role" not in titles

    def test_a_board_that_omits_is_listed_is_still_public(self) -> None:
        payload = {
            "jobs": [
                {
                    "id": "x",
                    "title": "Intern",
                    "jobUrl": "https://jobs.ashbyhq.com/testco/x",
                    "location": "Austin",
                }
            ]
        }
        case = case_for("ashby")
        assert len(case.source(FakeTransport([ScriptedResponse.json(payload)])).fetch_jobs()) == 1

    def test_secondary_locations_and_postal_address_are_merged(self) -> None:
        job = by_title("ashby", "Software Engineer Intern")
        assert job.location is not None
        for expected in ("San Francisco", "New York", "Remote - US"):
            assert expected in job.location

    def test_millisecond_published_at_is_accepted(self) -> None:
        job = by_title("ashby", "AI Engineer Intern")
        assert job.date_posted == datetime.fromtimestamp(1769728646, tz=UTC)


class TestSmartRecruiters:
    def test_pages_by_offset_in_page_size_steps(self) -> None:
        case = case_for("smartrecruiters")
        transport = case.transport()
        case.source(transport).fetch()
        offsets = [url.split("offset=")[1] for url in transport.urls]
        assert offsets == ["0", "100", "200"]
        assert all(f"limit={SR_PAGE_SIZE}" in url for url in transport.urls)

    def test_builds_the_public_apply_url_from_the_company_identifier(self) -> None:
        job = jobs_for("smartrecruiters")[0]
        assert job.url.startswith("https://jobs.smartrecruiters.com/TestCo/")
        assert job.url.rsplit("/", 1)[-1] == job.external_id

    def test_location_is_composed_from_city_region_country(self) -> None:
        assert jobs_for("smartrecruiters")[0].location == "Sydney, NSW, au"

    def test_remote_flag_is_reflected(self) -> None:
        remote = [job for job in jobs_for("smartrecruiters") if "Remote" in (job.location or "")]
        assert remote

    def test_employment_type_prefers_type_of_employment(self) -> None:
        assert jobs_for("smartrecruiters")[0].employment_type == "Intern"

    def test_stops_when_total_found_is_reached(self) -> None:
        case = case_for("smartrecruiters")
        transport = case.transport()
        case.source(transport).fetch()
        # 250 records at 100 per page = 3 requests, not a 4th probing request.
        assert transport.call_count == 3

    def test_a_short_page_ends_pagination(self) -> None:
        payload = {
            "offset": 0,
            "limit": 100,
            "totalFound": 9999,
            "content": [{"id": "1", "name": "Intern"}],
        }
        case = case_for("smartrecruiters")
        transport = FakeTransport(always=ScriptedResponse.json(payload))
        result = case.source(transport).fetch()
        assert transport.call_count == 1
        assert len(result.jobs) == 1

    def test_an_empty_first_page_ends_pagination(self) -> None:
        case = case_for("smartrecruiters")
        transport = FakeTransport(always=case.empty_response)
        assert case.source(transport).fetch().jobs == []
        assert transport.call_count == 1

    def test_missing_content_key_is_a_parse_error(self) -> None:
        case = case_for("smartrecruiters")
        with pytest.raises(ParseError, match="no 'content'"):
            case.source(FakeTransport([ScriptedResponse.json({"totalFound": 1})])).fetch_jobs()


class TestWorkdayPostedOn:
    REFERENCE = datetime(2026, 9, 26, 14, 30, 5, tzinfo=UTC)

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Posted Today", "2026-09-26"),
            ("Posted Yesterday", "2026-09-25"),
            ("Posted 5 Days Ago", "2026-09-21"),
            ("Posted 1 Day Ago", "2026-09-25"),
            ("Posted 30+ Days Ago", "2026-08-27"),
            ("posted 2 months ago", "2026-07-28"),  # 60 days back
        ],
    )
    def test_relative_text_is_converted(self, text: str, expected: str) -> None:
        result = parse_posted_on(text, now=self.REFERENCE)
        assert result is not None
        assert result.date().isoformat() == expected

    def test_result_is_truncated_to_the_day(self) -> None:
        """Otherwise an identical posting hashes differently every second."""
        result = parse_posted_on("Posted Today", now=self.REFERENCE)
        assert result == datetime(2026, 9, 26, tzinfo=UTC)

    @pytest.mark.parametrize("text", [None, "", "   ", "Posted recently", "Just posted!"])
    def test_unreadable_text_becomes_none(self, text: str | None) -> None:
        assert parse_posted_on(text, now=self.REFERENCE) is None


class TestWorkday:
    def test_posts_the_documented_cxs_body(self) -> None:
        case = case_for("workday")
        transport = case.transport()
        case.source(transport).fetch()
        assert transport.urls[0] == (
            "https://testco.wd1.myworkdayjobs.com/wday/cxs/testco/External/jobs"
        )
        bodies = transport.bodies()
        assert bodies[0] == {
            "appliedFacets": {},
            "limit": WD_PAGE_SIZE,
            "offset": 0,
            "searchText": "intern",
        }
        assert [body["offset"] for body in bodies] == [0, 20, 40]

    def test_search_text_is_configurable(self) -> None:
        from tests.scrapers.conftest import build_client

        from jobmonitor.scrapers import build_source

        case = case_for("workday")
        base = case.company()
        tuned = base.__class__(
            **{**base.to_dict(), "provider_config": {**case.config, "search_text": "student"}}
        )
        transport = case.transport()
        build_source(tuned, build_client(transport)).fetch()
        assert transport.bodies()[0]["searchText"] == "student"

    def test_url_is_built_from_host_locale_site_and_external_path(self) -> None:
        job = jobs_for("workday")[0]
        assert job.url.startswith(
            "https://testco.wd1.myworkdayjobs.com/en-US/External/job/Santa-Clara/"
        )

    def test_requisition_id_comes_from_bullet_fields(self) -> None:
        assert by_title("workday", "Software Engineering Intern").external_id == "JR10000"

    def test_requisition_id_falls_back_to_the_url_path(self) -> None:
        # bulletFields holds a location instead of an id for this fixture record.
        assert by_title("workday", "Intern Without Requisition Id").external_id == "JR10004"

    def test_relative_dates_are_resolved_against_an_injected_clock(self) -> None:
        from jobmonitor.scrapers.workday import WorkdaySource

        case = case_for("workday")
        from tests.scrapers.conftest import build_client

        source = WorkdaySource(
            case.company(),
            build_client(case.transport()),
            clock=lambda: datetime(2026, 9, 26, tzinfo=UTC),
        )
        jobs = {job.title: job for job in source.fetch_jobs()}
        assert jobs["Software Engineering Intern"].date_posted == datetime(2026, 9, 26, tzinfo=UTC)
        assert jobs["Firmware Engineering Intern"].date_posted == datetime(2026, 9, 25, tzinfo=UTC)
        assert jobs["Systems Engineering Intern"].date_posted == datetime(2026, 8, 27, tzinfo=UTC)

    def test_missing_job_postings_key_is_a_parse_error(self) -> None:
        case = case_for("workday")
        with pytest.raises(ParseError, match="no 'jobPostings'"):
            case.source(FakeTransport([ScriptedResponse.json({"total": 1})])).fetch_jobs()


class TestWorkable:
    def test_requests_the_documented_widget_endpoint(self) -> None:
        case = case_for("workable")
        transport = case.transport()
        case.source(transport).fetch()
        assert transport.urls == [
            "https://apply.workable.com/api/v1/widget/accounts/testco?details=true"
        ]

    def test_location_is_composed_and_remote_is_flagged(self) -> None:
        assert by_title("workable", "Software Engineer Intern").location == (
            "Austin, Texas, United States"
        )
        remote = by_title("workable", "Embedded Software Intern")
        assert remote.location is not None
        assert "Remote" in remote.location

    def test_description_requirements_and_benefits_are_concatenated(self) -> None:
        job = by_title("workable", "Software Engineer Intern")
        assert job.description is not None
        assert "Write services." in job.description
        assert "Python" in job.description

    def test_url_is_derived_from_the_shortcode_when_absent(self) -> None:
        assert by_title("workable", "Embedded Software Intern").url == (
            "https://apply.workable.com/testco/j/XYZ987WVU6/"
        )

    def test_shortcode_is_the_external_id(self) -> None:
        assert by_title("workable", "Software Engineer Intern").external_id == "ABC123DEF4"


class TestRippling:
    def test_requests_the_documented_board_endpoint(self) -> None:
        case = case_for("rippling")
        transport = case.transport()
        case.source(transport).fetch()
        assert transport.urls == ["https://api.rippling.com/platform/api/ats/v1/board/testco/jobs"]

    def test_url_is_derived_from_the_uuid_when_absent(self) -> None:
        assert by_title("rippling", "Backend Engineer Intern").url.endswith(
            "/testco/jobs/4ae0726b-e1d8-469d-b10d-6e0e800df88d"
        )

    def test_employment_type_accepts_a_string_or_an_object(self) -> None:
        assert by_title("rippling", "Frontend Software Engineer Intern").employment_type == "INTERN"
        assert by_title("rippling", "Backend Engineer Intern").employment_type == "Intern"

    def test_location_label_is_preferred_over_composition(self) -> None:
        assert by_title("rippling", "Frontend Software Engineer Intern").location == (
            "San Francisco, CA"
        )

    def test_location_is_composed_when_no_label_exists(self) -> None:
        job = by_title("rippling", "Backend Engineer Intern")
        assert job.location is not None
        assert job.location.startswith("New York, NY, US")
        assert "Remote" in job.location

    @pytest.mark.parametrize("key", ["jobs", "items", "results", "data"])
    def test_wrapped_array_responses_are_accepted(self, key: str) -> None:
        payload = {
            key: [{"uuid": "u1", "name": "Intern", "url": "https://ats.rippling.com/t/jobs/u1"}]
        }
        case = case_for("rippling")
        assert len(case.source(FakeTransport([ScriptedResponse.json(payload)])).fetch_jobs()) == 1


class TestJsonLdExtraction:
    @pytest.fixture(scope="class")
    @classmethod
    def html(cls) -> str:
        return load_fixture("json_ld", "careers.html")

    def test_finds_postings_in_a_graph_wrapper_and_a_top_level_array(self, html: str) -> None:
        titles = {posting.get("title") for posting in extract_job_postings(html)}
        assert "Software Engineer Intern" in titles
        assert "Robotics Software Intern" in titles
        assert "Director of Communications" in titles

    def test_ignores_non_jobposting_structured_data(self, html: str) -> None:
        for posting in extract_job_postings(html):
            assert posting.get("@type") == "JobPosting" or "JobPosting" in str(posting.get("@type"))

    def test_skips_a_malformed_block_without_losing_the_others(self, html: str) -> None:
        titles = {posting.get("title") for posting in extract_job_postings(html)}
        assert "Broken JSON Intern" not in titles
        assert len(extract_job_postings(html)) == 4

    def test_ignores_jobposting_shaped_data_in_a_plain_script_tag(self, html: str) -> None:
        titles = {posting.get("title") for posting in extract_job_postings(html)}
        assert "Not Structured Data" not in titles

    def test_no_structured_data_returns_empty(self) -> None:
        assert extract_job_postings("<html><body>nothing</body></html>") == []

    def test_handles_an_empty_document(self) -> None:
        assert extract_job_postings("") == []


class TestJsonLdSource:
    def test_relative_urls_are_resolved_against_the_configured_base(self) -> None:
        assert by_title("json_ld", "Software Engineer Intern").url == (
            "https://careers.testco.example.com/careers/software-engineer-intern-9001"
        )

    def test_absolute_urls_are_left_alone(self) -> None:
        assert by_title("json_ld", "Robotics Software Intern").url == (
            "https://careers.testco.example.com/careers/robotics-software-intern-9002"
        )

    def test_identifier_object_and_plain_string_both_work(self) -> None:
        assert by_title("json_ld", "Software Engineer Intern").external_id == "REQ-9001"
        assert by_title("json_ld", "Robotics Software Intern").external_id == "REQ-9002"

    def test_employment_type_list_is_joined(self) -> None:
        job = by_title("json_ld", "Software Engineer Intern")
        assert job.employment_type is not None
        assert "INTERN" in job.employment_type

    def test_postal_address_becomes_the_location(self) -> None:
        assert by_title("json_ld", "Software Engineer Intern").location == "Austin, TX, US"

    def test_telecommute_and_multiple_places_are_merged(self) -> None:
        job = by_title("json_ld", "Robotics Software Intern")
        assert job.location is not None
        assert "Boulder, CO" in job.location
        assert "Remote" in job.location

    def test_javascript_rendered_page_is_reported_as_needing_a_browser(self) -> None:
        case = case_for("json_ld")
        transport = FakeTransport(
            always=ScriptedResponse.text("<html><body><div id=app></div></body></html>")
        )
        with pytest.raises(ParseError, match="needs-browser"):
            case.source(transport).fetch_jobs()

    def test_empty_body_is_a_parse_error(self) -> None:
        case = case_for("json_ld")
        with pytest.raises(ParseError, match="empty body"):
            case.source(FakeTransport(always=ScriptedResponse.text("   "))).fetch_jobs()


class TestSimplifyFallback:
    def test_only_the_configured_company_is_returned(self) -> None:
        jobs = jobs_for("simplify_fallback")
        assert all(job.company == "TestCo" for job in jobs)
        titles = {job.title for job in jobs}
        # The feed also contains a SomeOtherCompany row with the same title; the
        # filter must be by employer, and that row's URL must not appear.
        assert all("other.example.com" not in job.url for job in jobs)
        assert titles == {"Software Engineer Intern", "Machine Learning Engineer Intern"}

    def test_alias_spellings_of_the_company_are_matched(self) -> None:
        # "Stripe, Inc." in the feed must match the configured name "Stripe".
        titles = {job.title for job in jobs_for("simplify_fallback")}
        assert "Machine Learning Engineer Intern" in titles

    def test_closed_and_hidden_listings_are_skipped(self) -> None:
        titles = {job.title for job in jobs_for("simplify_fallback")}
        assert "Closed Intern Role" not in titles
        assert "Hidden Intern Role" not in titles

    def test_tracking_parameters_do_not_affect_identity(self) -> None:
        job = by_title("simplify_fallback", "Software Engineer Intern")
        assert "utm_source" in job.url  # preserved for the user's click-through
        assert "utm" not in job_id(job)  # but not part of identity

    def test_locations_array_is_joined(self) -> None:
        job = by_title("simplify_fallback", "Software Engineer Intern")
        assert job.location is not None
        assert "San Francisco" in job.location
        assert "NYC" in job.location

    def test_reads_the_documented_raw_feed_url(self) -> None:
        case = case_for("simplify_fallback")
        transport = case.transport()
        case.source(transport).fetch()
        assert transport.urls[0].startswith("https://raw.githubusercontent.com/SimplifyJobs/")

    def test_non_array_response_is_a_parse_error(self) -> None:
        case = case_for("simplify_fallback")
        with pytest.raises(ParseError, match="expected a JSON array"):
            case.source(FakeTransport([ScriptedResponse.json({"a": 1})])).fetch_jobs()

    def test_feed_url_is_overridable_for_a_local_mirror(self) -> None:
        case = case_for("simplify_fallback")
        company = case.company()
        payload = json.loads(json.dumps(load_fixture("simplify", "listings.json")))
        from tests.scrapers.conftest import build_client

        from jobmonitor.scrapers import build_source

        overridden = company.__class__(
            **{
                **company.to_dict(),
                "provider_config": {
                    "company_names": ["Stripe"],
                    "feed_url": "https://mirror.test/l.json",
                },
            }
        )
        transport = FakeTransport([ScriptedResponse.json(payload)])
        build_source(overridden, build_client(transport)).fetch_jobs()
        assert transport.urls == ["https://mirror.test/l.json"]
