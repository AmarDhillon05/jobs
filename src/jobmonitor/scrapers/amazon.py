"""amazon.jobs - Amazon (and AWS, which posts on the same site).

Endpoint (public, unauthenticated - the JSON the amazon.jobs search page loads):

    GET https://www.amazon.jobs/en/search.json?base_query={query}
        &result_limit=100&offset={offset}&sort=recent

Response shape encoded by ``tests/fixtures/amazon/*.json``, captured live on
2026-09-27 and trimmed::

    {"hits": 323,
     "jobs": [{"id_icims": "10560398",
               "title": "Retail Vendor Manager Intern GBR",
               "job_path": "/en/jobs/10560398/retail-vendor-manager-intern-gbr",
               "posted_date": "September 25, 2026",
               "normalized_location": "London, England, GBR",
               "description_short": "...",
               "job_schedule_type": "full-time"}]}

Notes:

* ``result_limit`` tops out at 100, so a search is paged by ``offset`` until
  ``hits`` is reached.
* ``sort=recent`` puts the newest first; the adapter still reads every page, so
  correctness does not depend on the ordering holding.
* ``posted_date`` is a bare date ("September 25, 2026"); the one-day window reads
  it as "some time that day".
* Keyword-filtered server-side - Amazon lists tens of thousands of roles. Every
  term in ``_search.DEFAULT_QUERIES`` is searched and the results merged.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any, ClassVar
from urllib.parse import quote

from jobmonitor.errors import InvalidJobError, ParseError
from jobmonitor.models.job import Job
from jobmonitor.scrapers._parse import as_mapping, join_locations, text_of
from jobmonitor.scrapers._search import configured_queries
from jobmonitor.scrapers.base import JobSource, register

SITE = "https://www.amazon.jobs"
PAGE_SIZE = 100


@register
class AmazonJobsSource(JobSource):
    provider: ClassVar[str] = "amazon"
    required_config: ClassVar[tuple[str, ...]] = ()

    @property
    def queries(self) -> tuple[str, ...]:
        return configured_queries(self.config, "query")

    def page_url(self, offset: int, query: str | None = None) -> str:
        term = quote(query if query is not None else self.queries[0])
        return (
            f"{SITE}/en/search.json?base_query={term}"
            f"&result_limit={PAGE_SIZE}&offset={offset}&sort=recent"
        )

    @property
    def jobs_url(self) -> str:
        return self.page_url(0)

    def fetch_pages(self) -> Iterator[Sequence[Any]]:
        yield from self.search_all(self.queries, self._search)

    def _search(self, query: str) -> Iterator[Sequence[Any]]:
        offset = 0
        total: int | None = None
        while True:
            payload = self.client.get_json(self.page_url(offset, query))
            if not isinstance(payload, dict):
                raise ParseError(
                    f"{self.describe()}: expected a JSON object, got {type(payload).__name__}"
                )
            if payload.get("error"):
                raise ParseError(f"{self.describe()}: search returned error {payload['error']!r}")
            jobs = payload.get("jobs")
            if not isinstance(jobs, list):
                raise ParseError(
                    f"{self.describe()}: offset {offset} has no 'jobs' list "
                    f"(keys: {sorted(payload)[:8]})"
                )
            declared = payload.get("hits")
            if total is None and isinstance(declared, int) and declared > 0:
                total = declared
            if not jobs:
                return
            yield jobs
            offset += len(jobs)
            if total is not None:
                if offset >= total:
                    return
            elif len(jobs) < PAGE_SIZE:
                # Only without a declared total is a short page the end signal.
                # With one, a short page must not stop us: if the site ever lowers
                # its page size, stopping early would silently drop the rest - the
                # same shape of bug that cut every Workday board at 40 postings.
                return

    def normalize(self, raw_job: Any) -> Job:
        record = as_mapping(raw_job)
        if not record:
            raise InvalidJobError(f"{self.describe()}: posting was not an object")
        path = text_of(record, "job_path")
        if not path:
            raise InvalidJobError(
                f"{self.describe()}: posting {record.get('id_icims')} has no path"
            )

        return Job(
            company=self.company.company,
            title=text_of(record, "title") or "",
            url=path if path.startswith("http") else f"{SITE}{path}",
            source=self.provider,
            location=join_locations([text_of(record, "normalized_location", "location")]),
            external_id=text_of(record, "id_icims", "id"),
            date_posted=record.get("posted_date"),
            description=text_of(record, "description_short", "description"),
            employment_type=text_of(record, "job_schedule_type"),
        )


__all__: Sequence[str] = ("PAGE_SIZE", "AmazonJobsSource")
