"""Eightfold AI career sites - Microsoft, Qualcomm, Morgan Stanley, Millennium.

Endpoint (public, unauthenticated - the same call each company's own careers page
makes from the browser):

    GET https://{host}/api/pcsx/search?domain={domain}&query={query}
        &location=&start={offset}&sort_by=relevance

Response shape encoded by ``tests/fixtures/eightfold/*.json``, captured live from
``apply.careers.microsoft.com`` on 2026-09-27 and trimmed::

    {"status": 200,
     "data": {"count": 98,
              "positions": [{"id": 1970393556992502,
                             "displayJobId": "200054934",
                             "name": "Product Design INTERN",
                             "locations": ["India, Multiple Locations, ..."],
                             "postedTs": 1789031303,
                             "department": "Product Design",
                             "workLocationOption": "onsite",
                             "positionUrl": "/careers/job/1970393556992502"}]}}

Notes:

* **Why ``pcsx`` and not ``/api/apply/v2/jobs``.** Tenants that have moved to
  Eightfold's newer "PCSX" experience answer the older v2 endpoint with
  ``403 {"message": "Not authorized for PCSX"}``. That is not a bot wall - it is
  the server saying "use the new API", which is what the careers page itself
  does. ``api="v2"`` in the config selects the older endpoint for tenants that
  have not migrated.
* The page size is fixed server-side at 10; ``count`` is the total, so pages are
  requested until it is reached.
* ``postedTs`` is epoch **seconds** - a real timestamp, which the one-day window
  can use directly.
* The search is keyword-filtered server-side: these boards hold thousands of
  roles. Every term in ``_search.DEFAULT_QUERIES`` is searched and the results
  merged, because one keyword measurably misses roles (see ``_search``).
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any, ClassVar
from urllib.parse import quote

from jobmonitor.errors import InvalidJobError, ParseError
from jobmonitor.models.job import Job
from jobmonitor.scrapers._parse import as_mapping, as_sequence, join_locations, text_of
from jobmonitor.scrapers._search import configured_queries
from jobmonitor.scrapers.base import JobSource, register

PAGE_SIZE = 10


@register
class EightfoldSource(JobSource):
    provider: ClassVar[str] = "eightfold"
    required_config: ClassVar[tuple[str, ...]] = ("host", "domain")

    @property
    def host(self) -> str:
        return self.config_str("host")

    @property
    def domain(self) -> str:
        return self.config_str("domain")

    @property
    def queries(self) -> tuple[str, ...]:
        return configured_queries(self.config, "query")

    @property
    def api(self) -> str:
        return str(self.config.get("api", "pcsx"))

    def page_url(self, start: int, query: str | None = None) -> str:
        query = quote(query if query is not None else self.queries[0])
        if self.api == "v2":
            return (
                f"https://{self.host}/api/apply/v2/jobs?domain={self.domain}"
                f"&query={query}&start={start}&num={PAGE_SIZE}&sort_by=relevance"
            )
        return (
            f"https://{self.host}/api/pcsx/search?domain={self.domain}"
            f"&query={query}&location=&start={start}&sort_by=relevance"
        )

    @property
    def jobs_url(self) -> str:
        return self.page_url(0)

    def fetch_pages(self) -> Iterator[Sequence[Any]]:
        yield from self.search_all(self.queries, self._search)

    def _search(self, query: str) -> Iterator[Sequence[Any]]:
        start = 0
        total: int | None = None
        while True:
            payload = self.client.get_json(self.page_url(start, query))
            if not isinstance(payload, dict):
                raise ParseError(
                    f"{self.describe()}: expected a JSON object, got {type(payload).__name__}"
                )
            # PCSX nests under "data"; v2 returns the fields at the top level.
            data = as_mapping(payload.get("data")) or payload
            positions = data.get("positions")
            if not isinstance(positions, list):
                raise ParseError(
                    f"{self.describe()}: response at start={start} has no 'positions' "
                    f"list (keys: {sorted(data)[:8]})"
                )
            declared = data.get("count")
            if total is None and isinstance(declared, int) and declared > 0:
                total = declared
            if not positions:
                return
            yield positions
            start += len(positions)
            if total is not None and start >= total:
                return

    def normalize(self, raw_job: Any) -> Job:
        record = as_mapping(raw_job)
        if not record:
            raise InvalidJobError(f"{self.describe()}: position was not an object")

        path = text_of(record, "positionUrl", "canonicalPositionUrl")
        identifier = text_of(record, "id")
        if not path and identifier:
            path = f"/careers/job/{identifier}"
        if not path:
            raise InvalidJobError(f"{self.describe()}: position has neither url nor id")
        url = path if path.startswith("http") else f"https://{self.host}{path}"

        locations = [text_of({"v": value}, "v") for value in as_sequence(record.get("locations"))]
        if not any(locations):
            locations = [text_of(record, "location")]
        if (text_of(record, "workLocationOption") or "").casefold() == "remote":
            locations.append("Remote")

        return Job(
            company=self.company.company,
            title=text_of(record, "name", "title") or "",
            url=url,
            source=self.provider,
            location=join_locations(locations),
            external_id=text_of(record, "displayJobId", "atsJobId", "id"),
            date_posted=record.get("postedTs") or record.get("t_create"),
            description=text_of(record, "job_description", "description"),
            employment_type=text_of(record, "type", "employmentType"),
        )


__all__: Sequence[str] = ("PAGE_SIZE", "EightfoldSource")
