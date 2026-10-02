"""Event adapters (Level 2): event_page, avature_events, luma, sitemap_watch.

Every fixture under ``tests/fixtures/events/`` is a live capture from
2026-10-02 with scripts and styles stripped (Luma's ``__NEXT_DATA__`` payload is
kept). The clock is pinned to that day so "past" is deterministic.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any

import pytest
from tests.conftest import make_company
from tests.scrapers.conftest import TEST_HTTP, build_client
from tests.support.http import FakeTransport, RecordingSleeper, ScriptedResponse, load_fixture_bytes

from jobmonitor.errors import ParseError, ProviderConfigError
from jobmonitor.filtering.location import Region, classify_location
from jobmonitor.http import HttpClient, HttpRequest
from jobmonitor.models.health import ScraperStatus
from jobmonitor.scrapers import EVENT_PROVIDERS, build_source, registered_providers, safe_fetch
from jobmonitor.scrapers.base import JobSource
from jobmonitor.scrapers.events._common import (
    find_date,
    format_day,
    is_past,
    slug_title,
    trim_date,
    with_date,
)

pytestmark = pytest.mark.scrapers

NOW = datetime(2026, 10, 2, 18, 0, tzinfo=UTC)
TODAY = NOW.date()


def page(name: str) -> str:
    return load_fixture_bytes("events", name).decode("utf-8")


def html(name: str) -> ScriptedResponse:
    return ScriptedResponse.text(page(name))


def source(
    provider: str, transport: FakeTransport, name: str = "TestCo", **config: Any
) -> JobSource:
    company = make_company(name, provider=provider, config=config)
    src = build_source(company, build_client(transport))
    src.clock = lambda: NOW  # type: ignore[attr-defined]
    return src


def assert_event_contract(jobs: list[Any], *, source_name: str) -> None:
    """What every event adapter guarantees the pipeline (ARCHITECTURE.md, Events)."""
    assert jobs, "fixture should yield events"
    ids = [job.external_id for job in jobs]
    assert len(ids) == len(set(ids)), "duplicate events"
    for job in jobs:
        assert job.title and job.url.startswith("https://")
        assert job.external_id and job.external_id.startswith("event:")
        assert job.date_posted is None, "an event's date is when it happens, not when posted"
        assert job.source == source_name


def test_event_adapters_are_registered() -> None:
    assert set(EVENT_PROVIDERS) <= set(registered_providers())


# ------------------------------------------------------------------ event_page


JANE_STREET = {
    "url": "https://www.janestreet.com/join-jane-street/programs-and-events/",
    "link_pattern": "/programs-and-events/[a-z0-9-]+/?$",
}


class TestEventPage:
    def test_jane_street_programs_page(self) -> None:
        src = source("event_page", FakeTransport([html("janestreet_programs.html")]), **JANE_STREET)
        jobs = src.fetch_jobs()
        assert_event_contract(jobs, source_name="event_page")
        titles = [job.title for job in jobs]
        assert len(jobs) == 12
        # The card's <h3> name, not its <h4>PROGRAM label or <h6> status.
        assert {"Bridge", "FOCUS", "INSIGHT", "Graduate Research Fellowship"} <= set(titles)
        assert "PROGRAM" not in titles
        bridge = next(job for job in jobs if job.title == "Bridge")
        assert (
            bridge.url == "https://www.janestreet.com/join-jane-street/programs-and-events/bridge/"
        )
        assert "first- and second-year" in (bridge.description or "")
        assert "ACCEPTING APPLICATIONS" in (bridge.description or "")

    def test_ids_are_stable_across_fetches(self) -> None:
        def run() -> list[str | None]:
            src = source(
                "event_page", FakeTransport([html("janestreet_programs.html")]), **JANE_STREET
            )
            return [job.external_id for job in src.fetch_jobs()]

        assert run() == run()

    def test_generic_link_text_takes_the_cards_title_and_date(self) -> None:
        # Anthropic: each card's only link says "Learn more about this event".
        src = source(
            "event_page",
            FakeTransport([html("anthropic_events.html")]),
            url="https://www.anthropic.com/events",
            link_pattern="anthropic\\.com/events/[a-z0-9-]+",
        )
        titles = [job.title for job in src.fetch_jobs()]
        assert "Claude Founder House San Francisco · Oct 6, 2026" in titles
        assert not any("Learn more" in title for title in titles)

    def test_a_title_naming_a_place_abroad_marks_the_event_outside_the_us(self) -> None:
        src = source(
            "event_page",
            FakeTransport([html("anthropic_events.html")]),
            url="https://www.anthropic.com/events",
            link_pattern="anthropic\\.com/events/[a-z0-9-]+",
        )
        by_title = {job.title.split(" · ")[0]: job for job in src.fetch_jobs()}
        stockholm = by_title["Claude Founder House Stockholm"]
        assert classify_location(stockholm.location) is Region.NON_US
        assert by_title["Claude Founder House San Francisco"].location is None  # kept

    def test_past_events_are_skipped(self) -> None:
        src = source(
            "event_page",
            FakeTransport([html("gresearch_events.html")]),
            url="https://www.gresearch.com/events/",
            link_pattern="gresearch\\.com/events/[a-z0-9-]+/?$",
        )
        jobs = src.fetch_jobs()
        assert [job.title for job in jobs] == [
            "Berkeley quant challenge 2026 · Oct 8, 2026",
            "ML in PL conference 2026 · Oct 13, 2026",
            "Cambridge quant challenge 2026 · Oct 13, 2026",
            "SRECon 2026 · Oct 15, 2026",
            "Oxford quant challenge 2026 · Dec 6, 2026",
            "NeurIPS 2026 · Dec 9, 2026",
        ]
        later = source(
            "event_page",
            FakeTransport([html("gresearch_events.html")]),
            url="https://www.gresearch.com/events/",
            link_pattern="gresearch\\.com/events/[a-z0-9-]+/?$",
        )
        later.clock = lambda: datetime(2026, 12, 1, tzinfo=UTC)  # type: ignore[attr-defined]
        assert len(later.fetch_jobs()) == 2

    def test_date_before_the_link(self) -> None:
        # Pinterest: name, dates, place, then a "Learn more" link per card.
        src = source(
            "event_page",
            FakeTransport([html("pinterest_events.html")]),
            url="https://www.pinterestcareers.com/events/",
            link_pattern="beamery\\.com/Pinterest/",
            date_position="before",
        )
        assert [job.title for job in src.fetch_jobs()] == [
            "Velocity 2026 · Oct 7, 2026",
            "Women in Product 2026 · Oct 13, 2026",
            "AISES 2026 · Oct 15, 2026",
        ]

    def test_whole_card_link_text_is_cut_back_to_the_name(self) -> None:
        # Together AI: the link wraps the card: "The Science of Speed Oct . 09 , 2026 ...".
        src = source(
            "event_page",
            FakeTransport([html("together_events.html")]),
            url="https://www.together.ai/events",
            link_pattern="^(?!https://www\\.together\\.ai/(?!events/)).",
            require_date=True,
        )
        titles = [job.title for job in src.fetch_jobs()]
        assert "The Science of Speed · Oct 9, 2026" in titles
        assert all(len(title) < 80 for title in titles)

    def test_require_date_separates_cards_from_navigation(self) -> None:
        # Scale lists external conference sites; navigation links have no date.
        config = {
            "url": "https://scale.com/events",
            "link_pattern": "^(?!https://(?:[a-z]+\\.)?scale\\.com/(?!events/)).",
        }
        loose = source("event_page", FakeTransport([html("scale_events.html")]), **config)
        strict = source(
            "event_page", FakeTransport([html("scale_events.html")]), **config, require_date=True
        )
        strict_titles = [job.title for job in strict.fetch_jobs()]
        assert len(strict_titles) == 19
        assert "AWS re:INVENT · Nov 30, 2026" in strict_titles
        assert len(loose.fetch_jobs()) > len(strict_titles)

    def test_exclude_patterns(self) -> None:
        src = source(
            "event_page",
            FakeTransport([html("janestreet_programs.html")]),
            **JANE_STREET,
            exclude_link_pattern="/amp/",
            exclude_titles="^WiSE$",
        )
        titles = {job.title for job in src.fetch_jobs()}
        assert "AMP" not in titles and "WiSE" not in titles
        assert len(titles) == 10

    def test_several_links_to_one_event_are_one_event(self) -> None:
        body = (
            "<html><body>"
            '<a href="/events/a"><img src="x.png"></a>'
            '<a href="/events/a"><h3>Alpha Summit</h3></a>'
            '<a href="/events/a">Learn more</a>'
            "<p>November 4, 2026 · New York, NY</p>"
            '<a href="/events/b"><h3>Beta Meetup</h3></a><p>Online</p>'
            "</body></html>"
        )
        src = source(
            "event_page",
            FakeTransport([ScriptedResponse.text(body)]),
            url="https://example.com/events",
            link_pattern="/events/[a-z]+$",
        )
        jobs = src.fetch_jobs()
        assert [(job.title, job.location) for job in jobs] == [
            ("Alpha Summit · Nov 4, 2026", "New York, NY"),
            ("Beta Meetup", "Online"),
        ]

    def test_config_is_validated(self) -> None:
        with pytest.raises(ProviderConfigError, match="link_pattern"):
            source("event_page", FakeTransport([]), url="https://example.com/events")
        with pytest.raises(ProviderConfigError, match="bad link_pattern"):
            source("event_page", FakeTransport([]), url="https://example.com/e", link_pattern="([")

    def test_a_non_html_answer_fails_the_source(self) -> None:
        src = source(
            "event_page", FakeTransport([ScriptedResponse.text('{"error": "gone"}')]), **JANE_STREET
        )
        with pytest.raises(ParseError):
            src.fetch_jobs()

    def test_an_empty_page_is_empty_not_failed(self) -> None:
        src = source(
            "event_page",
            FakeTransport([ScriptedResponse.text("<html><body>No events</body></html>")]),
            **JANE_STREET,
        )
        result = safe_fetch(src)
        assert result.health is not None and result.health.status is ScraperStatus.EMPTY

    def test_transient_errors_are_retried(self) -> None:
        transport = FakeTransport(
            [
                ScriptedResponse.error(500),
                ScriptedResponse.error(500),
                html("janestreet_programs.html"),
            ]
        )
        assert len(source("event_page", transport, **JANE_STREET).fetch_jobs()) == 12
        assert transport.call_count == 3

    def test_rate_limit_is_respected_then_retried(self) -> None:
        transport = FakeTransport(
            [
                ScriptedResponse.error(429, headers={"Retry-After": "2"}),
                html("janestreet_programs.html"),
            ]
        )
        sleeper = RecordingSleeper()
        company = make_company("TestCo", provider="event_page", config=JANE_STREET)
        client = HttpClient(TEST_HTTP, transport=transport, sleep=sleeper)
        src = build_source(company, client)
        src.clock = lambda: NOW  # type: ignore[attr-defined]
        assert len(src.fetch_jobs()) == 12
        assert sleeper.delays and sleeper.delays[0] >= 2

    def test_a_permanent_failure_is_a_health_record_not_a_crash(self) -> None:
        src = source("event_page", FakeTransport(always=ScriptedResponse.error(403)), **JANE_STREET)
        result = safe_fetch(src)
        assert result.health is not None and not result.health.ok


# -------------------------------------------------------------- avature_events


BLOOMBERG_P1 = "https://bloomberg.avature.net/events/EventsList"
BLOOMBERG_P2 = "https://bloomberg.avature.net/events/EventsList/?jobRecordsPerPage=9&jobOffset=9"


def bloomberg_router(request: HttpRequest) -> ScriptedResponse:
    if request.url == BLOOMBERG_P1:
        return html("bloomberg_events_p1.html")
    if request.url == BLOOMBERG_P2:
        return html("bloomberg_events_p2.html")
    raise AssertionError(f"unexpected request {request.url}")


class TestAvatureEvents:
    def test_two_sigma_events(self) -> None:
        src = source(
            "avature_events",
            FakeTransport([html("twosigma_events.html")]),
            "Two Sigma",
            url="https://careers.twosigma.com/events/SearchEvents?eventRecordsPerPage=50",
        )
        jobs = src.fetch_jobs()
        assert_event_contract(jobs, source_name="avature_events")
        # The Grace Hopper brunch (21 Sep) is over; the UChicago meet & greet is not.
        assert [job.title for job in jobs] == [
            "University of Chicago: Two Sigma's Quant Research Meet & Greet Fall 2026 · Oct 5, 2026"
        ]
        job = jobs[0]
        assert job.external_id == "event:14284"
        assert job.location == "United States - IL Chicago"
        assert job.url == "https://careers.twosigma.com/events/EventDetail?jobId=14284"

    def test_bloomberg_follows_pagination_politely(self) -> None:
        transport = FakeTransport(router=bloomberg_router)
        src = source("avature_events", transport, "Bloomberg", url=BLOOMBERG_P1)
        sleeps: list[float] = []
        src.sleep = sleeps.append  # type: ignore[attr-defined]
        jobs = src.fetch_jobs()
        assert transport.urls == [BLOOMBERG_P1, BLOOMBERG_P2]
        assert sleeps == [1.0]
        titles = [job.title for job in jobs]
        # Page 2 (the last) was read, and the past Sep 8 session was skipped.
        assert any("Client Financi" in title for title in titles)
        assert not any("Japanese Speakers" in title for title in titles)
        assert "Discover Bloomberg: Geneva · Oct 26, 2026" in titles
        assert len(jobs) == 10

    def test_undated_on_demand_sessions_are_kept(self) -> None:
        src = source("avature_events", FakeTransport(router=bloomberg_router), url=BLOOMBERG_P1)
        src.sleep = lambda _delay: None  # type: ignore[attr-defined]
        on_demand = [job for job in src.fetch_jobs() if job.description == "On Demand"]
        assert len(on_demand) == 6  # 4 on page 1, 2 on page 2

    def test_a_non_html_answer_fails_the_source(self) -> None:
        src = source(
            "avature_events", FakeTransport([ScriptedResponse.text("{}")]), url=BLOOMBERG_P1
        )
        with pytest.raises(ParseError):
            src.fetch_jobs()


# ------------------------------------------------------------------------ luma


def luma_api(entries: list[Any], *, cursor: str | None = None) -> ScriptedResponse:
    return ScriptedResponse.json(
        {"entries": entries, "has_more": cursor is not None, "next_cursor": cursor}
    )


class TestLuma:
    def test_calendar_page_events(self) -> None:
        transport = FakeTransport([html("luma_databricks.html")])
        src = source("luma", transport, "Databricks", calendar="databricks")
        jobs = src.fetch_jobs()
        assert transport.urls == ["https://luma.com/databricks"]
        assert_event_contract(jobs, source_name="luma")
        assert len(jobs) == 7
        dc = jobs[0]
        assert dc.title == "Databricks DevConnect | Washington, DC · Oct 14, 2026"
        assert dc.url == "https://luma.com/p29ve9t1"
        assert dc.location == "Washington, DC, United States"
        bangalore = next(job for job in jobs if "Bangalore" in job.title)
        assert bangalore.location == "Bengaluru, India"  # country not repeated

    def test_has_more_reads_the_rest_from_the_api(self) -> None:
        extra = json.loads(load_fixture_bytes("events", "luma_api_page.json"))
        later = dict(extra["entries"][0])
        later["event"] = {
            **later["event"],
            "api_id": "evt-later",
            "name": "Seattle | Claude Builders",
            "url": "claude-later",
            "start_at": "2026-11-20T02:00:00.000Z",
            "end_at": "2026-11-20T04:00:00.000Z",
            "timezone": "America/Los_Angeles",
            "location_type": "offline",
            "geo_address_info": {"city_state": "Seattle, WA", "country": "United States"},
        }
        transport = FakeTransport(
            [
                html("luma_claudecommunity.html"),
                luma_api(extra["entries"], cursor="abc"),
                luma_api([later]),
            ]
        )
        src = source("luma", transport, "Anthropic", calendar="claudecommunity")
        jobs = src.fetch_jobs()
        assert transport.urls[1].startswith(
            "https://api.lu.ma/calendar/get-items?calendar_api_id=cal-TOpA5LAFfuDeFpu&period=future"
        )
        assert transport.urls[2].endswith("&pagination_cursor=abc")
        # The API repeats the embedded first entries; they are one event each.
        assert len(jobs) == 15
        seattle = next(job for job in jobs if job.external_id == "event:evt-later")
        # Local date in the event's own time zone: 02:00Z on the 20th is the 19th in Seattle.
        assert seattle.title == "Seattle | Claude Builders · Nov 19, 2026"

    def test_past_events_are_dropped_not_counted_as_malformed(self) -> None:
        src = source("luma", FakeTransport([html("luma_databricks.html")]), calendar="databricks")
        src.clock = lambda: datetime(2026, 10, 20, tzinfo=UTC)  # type: ignore[attr-defined]
        result = src.fetch()
        assert result.malformed == 0
        assert len(result.jobs) < 7

    def test_online_events(self) -> None:
        body = page("luma_databricks.html").replace(
            '"location_type":"offline"', '"location_type":"online"', 1
        )
        src = source("luma", FakeTransport([ScriptedResponse.text(body)]), calendar="databricks")
        assert src.fetch_jobs()[0].location == "Online"

    def test_an_event_page_is_not_a_calendar(self) -> None:
        body = '<script id="__NEXT_DATA__" type="application/json">{"props":{"pageProps":{"initialData":{"kind":"event","data":{}}}}}</script>'
        src = source("luma", FakeTransport([ScriptedResponse.text(body)]), calendar="brex")
        with pytest.raises(ParseError, match="not a Luma calendar"):
            src.fetch_jobs()

    def test_a_page_without_data_fails(self) -> None:
        src = source("luma", FakeTransport([ScriptedResponse.text("<html></html>")]), calendar="x")
        with pytest.raises(ParseError):
            src.fetch_jobs()


# --------------------------------------------------------------- sitemap_watch


CITADEL = {
    "url": "https://www.citadel.com/page-sitemap.xml",
    "path_prefix": "/careers/programs-and-events/",
}


class TestSitemapWatch:
    def test_citadel_programs(self) -> None:
        src = source(
            "sitemap_watch",
            FakeTransport([ScriptedResponse.text(page("citadel_page_sitemap.xml"))]),
            "Citadel",
            **CITADEL,
        )
        jobs = src.fetch_jobs()
        assert_event_contract(jobs, source_name="sitemap_watch")
        titles = {job.title for job in jobs}
        assert {
            "Discover Citadel",
            "PhD Summit",
            "PhD Summit · Apply for Citadel PhD Summit London",
            "The Trading Invitational",
            "GQS PhD Fellowship",
        } <= titles
        assert all(job.employment_type == "Program" for job in jobs)
        # The listing page itself is not a program.
        assert "https://www.citadel.com/careers/programs-and-events/" not in {j.url for j in jobs}
        assert len(jobs) == 16

    def test_exclude_pattern(self) -> None:
        src = source(
            "sitemap_watch",
            FakeTransport([ScriptedResponse.text(page("citadelsecurities_page_sitemap.xml"))]),
            **{**CITADEL, "url": "https://www.citadelsecurities.com/page-sitemap.xml"},
            exclude_pattern="conference-travel-grant",
        )
        titles = {job.title for job in src.fetch_jobs()}
        assert "The Citadel Securities Quant Invitational" in titles
        assert not any("Travel Grant" in title for title in titles)

    def test_not_a_sitemap(self) -> None:
        src = source(
            "sitemap_watch", FakeTransport([ScriptedResponse.text("<html>403</html>")]), **CITADEL
        )
        with pytest.raises(ParseError, match="sitemap"):
            src.fetch_jobs()


# --------------------------------------------------------------------- helpers


class TestDates:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("November 4, 2026", date(2026, 11, 4)),
            ("Nov 4-6, 2026", date(2026, 11, 4)),
            ("Nov. 30 \u2013 Dec. 2, 2026", date(2026, 11, 30)),
            ("Sep . 30 - Oct 01 , 2026", date(2026, 9, 30)),
            ("4 November 2026", date(2026, 11, 4)),
            ("2026-11-04", date(2026, 11, 4)),
            ("11/04/2026", date(2026, 11, 4)),
            ("October 07 - October 09 2026", date(2026, 10, 7)),
            ("Oct 14", date(2026, 10, 14)),
            # No year: the occurrence nearest today (Oct 2, 2026).
            ("Jan 30", date(2027, 1, 30)),
            ("Sep 8", date(2026, 9, 8)),
        ],
    )
    def test_formats(self, text: str, expected: date) -> None:
        found = find_date(f"Event · {text} · New York", today=TODAY)
        assert found is not None and found[0] == expected

    @pytest.mark.parametrize(
        "text", ["Room 12", "Top 10 tips", "8:00 AM - 5:00 PM", "May the best"]
    )
    def test_non_dates(self, text: str) -> None:
        assert find_date(text, today=TODAY) is None

    def test_past(self) -> None:
        assert is_past(date(2026, 9, 29), now=NOW)
        assert not is_past(date(2026, 9, 30), now=NOW)  # within the grace for multi-day events
        assert not is_past(None, now=NOW)

    def test_title_helpers(self) -> None:
        assert format_day(date(2026, 11, 4)) == "Nov 4, 2026"
        assert with_date("Bridge", date(2026, 11, 4)) == "Bridge · Nov 4, 2026"
        assert with_date("Bridge Nov 4, 2026", date(2026, 11, 4)) == "Bridge Nov 4, 2026"
        assert trim_date(
            "The AI Conference Sep . 30 - Oct 01 , 2026 San Francisco", today=TODAY
        ) == ("The AI Conference")
        assert trim_date("October 28-29, 2026 GitHub Universe", today=TODAY) == "GitHub Universe"
        assert slug_title("https://x.com/careers/phd-summit/") == "PhD Summit"
        assert (
            slug_title("https://x.com/events/emnlp-2026-2026-10-24t16-00-00-000z") == "Emnlp 2026"
        )
