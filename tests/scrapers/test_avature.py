"""Avature (Bloomberg): sitemap discovery, internship slugs, paced detail pages.

Fixtures are live captures from bloomberg.avature.net on 2026-10-01. No
Bloomberg internship was open that day, so the two internship detail pages are
the captured page with only the title and location changed; the sitemap's
internship entries are added the same way. Everything else is verbatim.
"""

from __future__ import annotations

import pytest
from tests.conftest import make_company
from tests.scrapers.conftest import avature_transport, build_client
from tests.support.http import FakeTransport, ScriptedResponse, load_fixture_bytes

from jobmonitor.errors import ParseError
from jobmonitor.scrapers import build_source
from jobmonitor.scrapers.avature import parse_detail, parse_sitemap, slug_words

pytestmark = pytest.mark.scrapers

SITEMAP = load_fixture_bytes("avature", "sitemap.xml").decode()
DETAIL = load_fixture_bytes("avature", "detail_internal_communications.html").decode()


def source(transport: FakeTransport, **config: object):  # type: ignore[no-untyped-def]
    company = make_company(
        "Bloomberg",
        provider="avature",
        config={"host": "bloomberg.avature.net", "detail_delay_seconds": 0, **config},
    )
    return build_source(company, build_client(transport))


class TestSitemap:
    def test_lists_postings_and_skips_portal_pages(self) -> None:
        postings = parse_sitemap(SITEMAP)
        assert len(postings) == 13  # the 3 portal pages (AgentCreate, ...) are not jobs
        first = postings[0]
        assert first["url"].startswith("https://bloomberg.avature.net/careers/JobDetail/")
        assert first["id"].isdigit()
        assert first["lastmod"]

    @pytest.mark.parametrize(
        ("slug", "is_internship"),
        [
            ("2027-Software-Engineering-Intern-New-York", True),
            ("Data-Science-Internship-London", True),
            ("Software-Engineer-Co-op-Toronto", True),
            ("Internal-Communications-Specialist", False),
            ("Global-Financial-Crimes-and-International-Trade-Counsel", False),
            ("Senior-Software-Engineer-RDF-Infrastructure", False),
        ],
    )
    def test_only_whole_word_internships_are_fetched(self, slug: str, is_internship: bool) -> None:
        src = source(avature_transport())
        assert src.wanted({"slug": slug}) is is_internship
        assert ("co-op" in slug_words(slug)) is ("Co-op" in slug)


class TestDetailPage:
    def test_reads_title_location_reference_and_description(self) -> None:
        detail = parse_detail(DETAIL)
        assert detail["title"] == "Internal Communications Specialist"
        assert detail["location"] == "New York"
        assert detail["reference"] == "10054227"
        assert detail["description"] and detail["description"].startswith("Bloomberg")

    def test_a_title_may_itself_contain_a_dash(self) -> None:
        page = load_fixture_bytes("avature", "detail_intern_new_york.html").decode()
        assert parse_detail(page)["title"] == "2027 Software Engineering Intern - New York"


class TestFetching:
    def test_one_sitemap_request_then_only_internship_detail_pages(self) -> None:
        transport = avature_transport()
        result = source(transport).fetch()
        fetched = [r.url for r in transport.requests]
        assert fetched[0].endswith("/careers/sitemap.xml")
        assert sorted(u.rsplit("/", 1)[-1] for u in fetched[1:]) == ["45001", "45002", "45003"]
        by_id = {job.external_id: job for job in result.jobs}
        assert by_id["45001"].location == "New York"
        assert by_id["45002"].location == "London"
        assert by_id["45001"].url.endswith(
            "/JobDetail/2027-Software-Engineering-Intern-New-York/45001"
        )
        assert result.malformed == 1  # the co-op's page answered 404

    def test_detail_requests_are_paced(self) -> None:
        pauses: list[float] = []
        src = source(avature_transport(), detail_delay_seconds=1.5)
        src.sleep = pauses.append  # type: ignore[method-assign]
        src.fetch()
        assert pauses == [1.5, 1.5]  # between the 3 detail pages, not before the first

    def test_custom_title_words(self) -> None:
        transport = avature_transport()
        result = source(transport, title_words=["co-op"]).fetch()
        assert len(transport.requests) == 2
        assert result.jobs == [] and result.malformed == 1

    def test_something_that_is_not_a_sitemap_is_a_parse_error(self) -> None:
        transport = FakeTransport(always=ScriptedResponse.text("<html>maintenance</html>"))
        with pytest.raises(ParseError, match="not a sitemap"):
            source(transport).fetch()
