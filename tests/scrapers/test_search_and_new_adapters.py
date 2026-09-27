"""Behaviour the shared contract battery cannot see.

Each test here pins something found during live validation on 2026-09-27: a
discovery bug, a site quirk, or a design decision that a fixture replay would
never exercise.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from tests.conftest import make_company
from tests.scrapers.conftest import build_client, case_for
from tests.support.http import FakeTransport, ScriptedResponse, load_fixture

from jobmonitor.discovery import careers_url_for, detect_provider
from jobmonitor.errors import ParseError
from jobmonitor.http import HttpRequest
from jobmonitor.models.health import ScraperStatus
from jobmonitor.scrapers import build_source
from jobmonitor.scrapers._search import DEFAULT_QUERIES, configured_queries
from jobmonitor.scrapers.atlassian import parse_updated
from jobmonitor.scrapers.base import safe_fetch
from jobmonitor.scrapers.google import extract_jobs_block
from jobmonitor.scrapers.talentbrew import parse_cards

pytestmark = pytest.mark.scrapers


class TestDiscoveryFixes:
    """Two bugs that sent real companies to endpoints that 404 / 422."""

    def test_lever_eu_region_is_kept(self) -> None:
        # Cirrus Logic and Quantinuum post on jobs.eu.lever.co; dropping the region
        # pointed them at the US API, which answers 404.
        guess = detect_provider("https://jobs.eu.lever.co/cirrus/2926421c-691a-434a-ae59")
        assert guess is not None
        assert guess.provider == "lever"
        assert guess.config == {"site": "cirrus", "region": "eu"}

    def test_lever_us_boards_carry_no_region(self) -> None:
        guess = detect_provider("https://jobs.lever.co/acme/0f1e2d3c")
        assert guess is not None and guess.config == {"site": "acme"}

    def test_lever_eu_careers_url_uses_the_eu_host(self) -> None:
        url = careers_url_for("lever", {"site": "cirrus", "region": "eu"})
        assert url == "https://jobs.eu.lever.co/cirrus"

    def test_workday_tenant_id_uses_underscores(self) -> None:
        # osv-cci.wd1.myworkdayjobs.com is tenant "osv_cci"; "osv-cci" returns 422.
        guess = detect_provider("https://osv-cci.wd1.myworkdayjobs.com/en-US/CCICareers/job/X_1")
        assert guess is not None
        assert guess.config["tenant"] == "osv_cci"
        assert guess.config["host"] == "osv-cci.wd1.myworkdayjobs.com"  # host keeps its hyphen

    def test_an_ordinary_workday_tenant_is_unchanged(self) -> None:
        guess = detect_provider(
            "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/job/X"
        )
        assert guess is not None and guess.config["tenant"] == "nvidia"


class TestConfiguredQueries:
    def test_defaults_to_every_internship_term(self) -> None:
        assert configured_queries({}) == DEFAULT_QUERIES
        assert "summer analyst" in DEFAULT_QUERIES  # how Goldman titles internships

    def test_a_list_overrides(self) -> None:
        assert configured_queries({"queries": ["internship"]}) == ("internship",)

    def test_legacy_single_string_keys_still_work(self) -> None:
        assert configured_queries({"keyword": "co-op"}, "keyword") == ("co-op",)

    def test_blank_and_duplicate_terms_are_dropped(self) -> None:
        assert configured_queries({"queries": ["intern", " ", "intern", "co-op"]}) == (
            "intern",
            "co-op",
        )

    def test_an_empty_list_falls_back_to_the_default(self) -> None:
        assert configured_queries({"queries": []}, default=("x",)) == ("x",)


def _amazon(queries: list[str]) -> object:
    return make_company("TestCo", provider="amazon", config={"queries": queries})


class TestMultiSearch:
    def test_every_term_is_searched_and_the_overlap_merged(self) -> None:
        seen: list[str] = []

        def route(request: HttpRequest) -> ScriptedResponse:
            term = request.url.split("base_query=")[1].split("&")[0]
            seen.append(term)
            # Both terms return the same first page: the overlap must collapse.
            return ScriptedResponse.json(load_fixture("amazon", "search_page2.json") | {"hits": 3})

        source = build_source(
            _amazon(["intern", "internship"]), build_client(FakeTransport(router=route))
        )
        jobs = source.fetch_jobs()
        assert seen == ["intern", "internship"]
        # Both searches return the same 3 distinct postings: 3 jobs, not 6.
        assert len(jobs) == 3

    def test_the_first_search_failing_fails_the_company(self) -> None:
        # That is how a site that is down or refusing us must show up.
        transport = FakeTransport(always=ScriptedResponse.error(503))
        result = safe_fetch(
            build_source(_amazon(["intern", "internship"]), build_client(transport))
        )
        assert result.health is not None
        assert result.health.status is ScraperStatus.FAILED

    def test_a_later_search_failing_keeps_what_was_found(self) -> None:
        """Observed live: one of Microsoft's searches 429'd and took every result with it."""

        def route(request: HttpRequest) -> ScriptedResponse:
            if "base_query=internship" in request.url:
                return ScriptedResponse.error(503)
            return ScriptedResponse.json(load_fixture("amazon", "search_page2.json") | {"hits": 3})

        result = safe_fetch(
            build_source(
                _amazon(["intern", "internship"]), build_client(FakeTransport(router=route))
            )
        )
        assert result.health is not None
        assert result.health.status is ScraperStatus.DEGRADED
        assert "'internship'" in (result.health.error or "")
        assert len(result.jobs) == 3  # everything the first search found
        assert result.ok  # degraded, not failed: the jobs found are still used

    def test_a_blocked_first_search_is_blocked_and_not_retried(self) -> None:
        transport = FakeTransport(always=ScriptedResponse.error(403))
        result = safe_fetch(
            build_source(_amazon(["intern", "internship"]), build_client(transport))
        )
        assert result.health is not None
        assert result.health.status is ScraperStatus.BLOCKED
        assert transport.call_count == 1

    def test_ibm_sends_a_multi_word_term_as_a_phrase(self) -> None:
        # simple_query_string ORs bare words: "summer analyst" would match every
        # analyst role at IBM.
        source = build_source(
            make_company("TestCo", provider="ibm", config={}), build_client(FakeTransport([]))
        )
        body = source.request_body(0, "summer analyst")  # type: ignore[attr-defined]
        term = body["query"]["bool"]["must"][0]["simple_query_string"]["query"]
        assert term == '"summer analyst"'
        assert (
            source.request_body(0, "intern")["query"]["bool"]["must"][0]["simple_query_string"][
                "query"
            ]
            == "intern"
        )  # type: ignore[attr-defined]


class TestWorkdayVariants:
    def test_myworkdaysite_job_links_use_the_recruiting_path(self) -> None:
        # Snap: the /{locale}/{site} form answers 500 on this host; /recruiting/... is 200.
        company = make_company(
            "Snap",
            provider="workday",
            config={"host": "wd1.myworkdaysite.com", "tenant": "snapchat", "site": "snap"},
        )
        source = build_source(company, build_client(case_for("workday").transport()))
        url = source.fetch_jobs()[0].url
        assert url.startswith("https://wd1.myworkdaysite.com/recruiting/snapchat/snap/job/")

    def test_myworkdayjobs_links_keep_the_locale_path(self) -> None:
        url = case_for("workday").source().fetch_jobs()[0].url
        assert url.startswith("https://testco.wd1.myworkdayjobs.com/en-US/External/job/")

    def test_a_tenant_can_override_its_search_terms(self) -> None:
        # RTX and Micron: "intern" matches past Workday's 2,000-result limit.
        company = make_company(
            "RTX",
            provider="workday",
            config={**case_for("workday").config, "queries": ["internship"]},
        )
        transport = case_for("workday").transport()
        build_source(company, build_client(transport)).fetch()
        assert {json.loads(r.body or b"{}")["searchText"] for r in transport.requests} == {
            "internship"
        }

    def test_the_default_stays_a_single_intern_search(self) -> None:
        transport = case_for("workday").transport()
        case_for("workday").source(transport).fetch()
        assert {json.loads(r.body or b"{}")["searchText"] for r in transport.requests} == {"intern"}


class TestLeverRegions:
    def test_eu_boards_use_the_eu_api(self) -> None:
        company = make_company(
            "Cirrus Logic", provider="lever", config={"site": "cirrus", "region": "eu"}
        )
        assert (
            build_source(company).jobs_url == "https://api.eu.lever.co/v0/postings/cirrus?mode=json"
        )  # type: ignore[attr-defined]

    def test_an_unknown_region_fails_loudly(self) -> None:
        company = make_company("X", provider="lever", config={"site": "x", "region": "mars"})
        with pytest.raises(ParseError, match="mars"):
            _ = build_source(company).jobs_url  # type: ignore[attr-defined]


class TestParsers:
    def test_atlassian_updated_date(self) -> None:
        assert parse_updated("2026-09-22 12:42 AM") == datetime(2026, 9, 22, 0, 42, tzinfo=UTC)
        assert parse_updated("2026-09-22 01:05 PM") == datetime(2026, 9, 22, 13, 5, tzinfo=UTC)

    @pytest.mark.parametrize("value", [None, "", "yesterday", "2026-13-40 99:99 XM"])
    def test_atlassian_unreadable_date_is_none(self, value: str | None) -> None:
        assert parse_updated(value) is None

    def test_google_finds_the_jobs_block_by_shape_not_by_key(self) -> None:
        # The fixture carries a ds:0 company list first, as the live page does.
        html = load_fixture("google", "results_page1.html")
        block = extract_jobs_block(html)
        assert block[2] == 9  # the declared total
        assert all(isinstance(job, list) for job in block[0])

    def test_google_page_without_a_jobs_block_is_a_parse_error(self) -> None:
        with pytest.raises(ParseError, match="AF_initDataCallback"):
            extract_jobs_block("<html><body>A redesign moved everything.</body></html>")

    def test_google_uses_the_publish_timestamp(self) -> None:
        # [13] is the field Google's own date sort follows; [12] is a batch import time.
        job = case_for("google").source().fetch_jobs()[0]
        assert job.date_posted is not None and job.date_posted.year == 2026

    def test_talentbrew_cards_parse_by_class(self) -> None:
        fragment = json.loads(json.dumps(load_fixture("talentbrew", "results_page1.json")))[
            "results"
        ]
        cards = parse_cards(fragment)
        assert cards[0]["title"] == "Software Developer Intern"
        assert cards[0]["href"].startswith("/job/")
        assert cards[0]["location"]
        assert any(card["href"] is None for card in cards)  # the deliberately broken card

    def test_goldman_graphql_errors_are_not_mistaken_for_success(self) -> None:
        transport = FakeTransport(
            always=ScriptedResponse.json(
                {"errors": [{"message": "Validation error"}], "data": None}
            )
        )
        result = safe_fetch(
            build_source(make_company("GS", provider="goldman", config={}), build_client(transport))
        )
        assert result.health is not None
        assert result.health.status is ScraperStatus.FAILED
        assert "Validation error" in (result.health.error or "")

    def test_eightfold_v2_tenants_use_the_older_endpoint(self) -> None:
        company = make_company(
            "Millennium",
            provider="eightfold",
            config={"host": "mlp.eightfold.ai", "domain": "mlp.com", "api": "v2"},
        )
        assert "/api/apply/v2/jobs?domain=mlp.com" in build_source(company).jobs_url  # type: ignore[attr-defined]


class TestShortPagesWithADeclaredTotal:
    """A short page must not end the fetch when the site declared more.

    Otherwise a site that lowers its page size would silently lose everything
    after page one - the shape of bug that cut every Workday board at 40.
    """

    def test_amazon_keeps_going_to_the_declared_total(self) -> None:
        page = load_fixture("amazon", "search_page1.json")  # 6 records, declares 9

        def route(request: HttpRequest) -> ScriptedResponse:
            offset = int(request.url.split("offset=")[1].split("&")[0])
            if offset == 0:
                return ScriptedResponse.json(page)
            return ScriptedResponse.json(load_fixture("amazon", "search_page2.json"))

        transport = FakeTransport(router=route)
        build_source(_amazon(["intern"]), build_client(transport)).fetch()
        assert transport.call_count == 2  # 6 < page size 100, but 6 < 9 declared
