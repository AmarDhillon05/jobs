"""Per-provider fixture plumbing for the Level 2 suite.

Each :class:`ProviderCase` knows how to build its adapter and how to serve its
saved fixture, which lets one contract test battery run against every provider
(`test_provider_contract.py`) while provider-specific behaviour keeps its own
focused tests.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest
from tests.conftest import make_company
from tests.support.http import FakeTransport, RecordingSleeper, ScriptedResponse, load_fixture

from jobmonitor.config import HttpSettings
from jobmonitor.http import HttpClient, HttpRequest
from jobmonitor.models.company import Company
from jobmonitor.scrapers import build_source
from jobmonitor.scrapers.base import JobSource

TEST_HTTP = HttpSettings(
    timeout_seconds=1.0, max_attempts=3, backoff_base_seconds=0.0, backoff_max_seconds=0.0
)


def build_client(transport: FakeTransport) -> HttpClient:
    """A client that never sleeps and never touches the network."""
    return HttpClient(TEST_HTTP, transport=transport, sleep=RecordingSleeper())


@dataclass(frozen=True)
class ProviderCase:
    """Everything the shared contract battery needs about one provider."""

    provider: str
    config: dict[str, Any]
    #: Builds the transport that serves this provider's saved fixture.
    transport: Callable[[], FakeTransport]
    #: Jobs expected from the fixture, after per-record skips and dedup.
    expected_jobs: int
    #: Records the fixture deliberately makes unparseable.
    expected_malformed: int
    #: A response with zero postings, in this provider's own shape.
    empty_response: ScriptedResponse
    #: Number of HTTP requests one full fetch should issue.
    expected_requests: int = 1
    #: Titles that must appear among the parsed jobs.
    must_contain_titles: tuple[str, ...] = ()
    #: True when the provider's own API paginates.
    paginates: bool = False
    #: A title that only exists on the LAST page. Proves no page was skipped.
    last_page_marker_title: str | None = None
    #: The total the fixture's own payload declares, for paginating providers.
    declared_total: int | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return self.provider

    def company(self, name: str = "TestCo") -> Company:
        return make_company(name, provider=self.provider, config=dict(self.config))

    def source(self, transport: FakeTransport | None = None) -> JobSource:
        transport = transport or self.transport()
        return build_source(self.company(), build_client(transport))


def _paged_router(
    pages: Sequence[Any], *, offset_from: Callable[[HttpRequest], int]
) -> Callable[[HttpRequest], ScriptedResponse]:
    """Serve page N based on the offset the adapter asked for.

    Returning an empty page for an out-of-range offset is what a real API does,
    and it is also how the "adapter ignores its own total" bug would show up.
    """

    def route(request: HttpRequest) -> ScriptedResponse:
        offset = offset_from(request)
        for page in pages:
            if page["offset"] == offset:
                return ScriptedResponse.json(page["payload"])
        return ScriptedResponse.json(page["empty"])

    return route


def greenhouse_transport() -> FakeTransport:
    return FakeTransport([ScriptedResponse.json(load_fixture("greenhouse", "board.json"))])


def lever_transport() -> FakeTransport:
    return FakeTransport([ScriptedResponse.json(load_fixture("lever", "postings.json"))])


def ashby_transport() -> FakeTransport:
    return FakeTransport([ScriptedResponse.json(load_fixture("ashby", "board.json"))])


def workable_transport() -> FakeTransport:
    return FakeTransport([ScriptedResponse.json(load_fixture("workable", "account.json"))])


def rippling_transport() -> FakeTransport:
    return FakeTransport([ScriptedResponse.json(load_fixture("rippling", "board.json"))])


def simplify_transport() -> FakeTransport:
    return FakeTransport([ScriptedResponse.json(load_fixture("simplify", "listings.json"))])


def json_ld_transport() -> FakeTransport:
    return FakeTransport([ScriptedResponse.text(load_fixture("json_ld", "careers.html"))])


def smartrecruiters_pages() -> list[dict[str, Any]]:
    return [
        {
            "offset": offset,
            "payload": load_fixture("smartrecruiters", f"postings_page{page}.json"),
            "empty": {"offset": offset, "limit": 100, "totalFound": 250, "content": []},
        }
        for page, offset in ((1, 0), (2, 100), (3, 200))
    ]


def smartrecruiters_transport() -> FakeTransport:
    def offset_from(request: HttpRequest) -> int:
        _, _, query = request.url.partition("?")
        params = dict(part.split("=", 1) for part in query.split("&") if "=" in part)
        return int(params.get("offset", 0))

    return FakeTransport(router=_paged_router(smartrecruiters_pages(), offset_from=offset_from))


def workday_pages() -> list[dict[str, Any]]:
    return [
        {
            "offset": offset,
            "payload": load_fixture("workday", f"jobs_page{page}.json"),
            "empty": {"total": 45, "jobPostings": []},
        }
        for page, offset in ((1, 0), (2, 20), (3, 40))
    ]


def workday_transport() -> FakeTransport:
    def offset_from(request: HttpRequest) -> int:
        body = json.loads(request.body or b"{}")
        return int(body.get("offset", 0))

    return FakeTransport(router=_paged_router(workday_pages(), offset_from=offset_from))


#: The fixture-derived expectations. Counts are asserted, not guessed: each
#: fixture deliberately contains non-internship roles, duplicates and broken
#: records so the contract battery has something real to prove.
PROVIDER_CASES: tuple[ProviderCase, ...] = (
    ProviderCase(
        provider="greenhouse",
        config={"board_token": "testco"},
        transport=greenhouse_transport,
        # 8 records: 1 dup, 1 no-url, 1 blank title, 1 bare string -> 4 jobs.
        expected_jobs=4,
        expected_malformed=3,
        empty_response=ScriptedResponse.json({"jobs": [], "meta": {"total": 0}}),
        must_contain_titles=("Software Engineer Intern, Summer 2027",),
    ),
    ProviderCase(
        provider="lever",
        config={"site": "testco"},
        transport=lever_transport,
        # 6 records: 1 no-url, 1 empty object, 1 null -> 3 jobs.
        expected_jobs=3,
        expected_malformed=3,
        empty_response=ScriptedResponse.json([]),
        must_contain_titles=("Software Engineer Intern (Summer 2027)",),
    ),
    ProviderCase(
        provider="ashby",
        config={"job_board_name": "testco"},
        transport=ashby_transport,
        # 5 records: 1 unlisted, 1 bad url -> 3 jobs.
        expected_jobs=3,
        expected_malformed=2,
        empty_response=ScriptedResponse.json({"apiVersion": "1", "jobs": []}),
        must_contain_titles=("Software Engineer Intern", "AI Engineer Intern"),
    ),
    ProviderCase(
        provider="smartrecruiters",
        config={"company_id": "TestCo"},
        transport=smartrecruiters_transport,
        # 250 records across 3 pages: 1 dup, 1 without an id -> 248 jobs.
        expected_jobs=248,
        expected_malformed=1,
        empty_response=ScriptedResponse.json(
            {"offset": 0, "limit": 100, "totalFound": 0, "content": []}
        ),
        expected_requests=3,
        paginates=True,
        must_contain_titles=("Software Engineer Intern",),
        last_page_marker_title="Cloud Engineer Intern 49",
        declared_total=250,
    ),
    ProviderCase(
        provider="workday",
        config={
            "host": "testco.wd1.myworkdayjobs.com",
            "tenant": "testco",
            "site": "External",
        },
        transport=workday_transport,
        # 45 records across 3 pages: 1 without externalPath -> 44 jobs.
        expected_jobs=44,
        expected_malformed=1,
        empty_response=ScriptedResponse.json({"total": 0, "jobPostings": []}),
        expected_requests=3,
        paginates=True,
        must_contain_titles=("Software Engineering Intern",),
        last_page_marker_title="Robotics Software Intern 4",
        declared_total=45,
    ),
    ProviderCase(
        provider="workable",
        config={"subdomain": "testco"},
        transport=workable_transport,
        # 4 records: the last has neither shortcode nor url -> 3 jobs.
        expected_jobs=3,
        expected_malformed=1,
        empty_response=ScriptedResponse.json({"name": "TestCo", "jobs": []}),
        must_contain_titles=("Software Engineer Intern", "Embedded Software Intern"),
    ),
    ProviderCase(
        provider="rippling",
        config={"board_slug": "testco"},
        transport=rippling_transport,
        # 4 records: the last has neither uuid nor url -> 3 jobs.
        expected_jobs=3,
        expected_malformed=1,
        empty_response=ScriptedResponse.json([]),
        must_contain_titles=("Frontend Software Engineer Intern",),
    ),
    ProviderCase(
        provider="json_ld",
        config={
            "url": "https://careers.testco.example.com/careers",
            "base_url": "https://careers.testco.example.com",
        },
        transport=json_ld_transport,
        # 4 JobPostings survive JSON parsing; 1 has no url -> 3 jobs.
        expected_jobs=3,
        expected_malformed=1,
        empty_response=ScriptedResponse.text(
            "<html><body>No structured data. jobposting</body></html>"
        ),
        must_contain_titles=("Software Engineer Intern", "Robotics Software Intern"),
    ),
    ProviderCase(
        provider="simplify_fallback",
        config={"company_names": ["Stripe"]},
        transport=simplify_transport,
        # 5 Stripe rows (a 6th belongs to another company): 1 closed, 1 hidden,
        # 1 without a url -> 2 jobs.
        expected_jobs=2,
        expected_malformed=3,
        empty_response=ScriptedResponse.json([]),
        must_contain_titles=("Software Engineer Intern",),
    ),
)

CASES_BY_PROVIDER: dict[str, ProviderCase] = {case.provider: case for case in PROVIDER_CASES}


@pytest.fixture(params=PROVIDER_CASES, ids=lambda case: case.id)
def provider_case(request: pytest.FixtureRequest) -> ProviderCase:
    return request.param


def case_for(provider: str) -> ProviderCase:
    return CASES_BY_PROVIDER[provider]
