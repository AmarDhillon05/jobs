"""iCIMS "Jibe" career sites - AMD, Susquehanna (SIG).

Endpoint (public, unauthenticated - what the careers site's own search calls):

    GET https://{host}/api/jobs?keywords={keywords}&page={n}

Response shape encoded by ``tests/fixtures/jibe/*.json``, captured live from
``careers.amd.com`` on 2026-09-27 and trimmed::

    {"totalCount": 110, "count": 110,
     "jobs": [{"data": {"slug": "90429", "req_id": "90429",
                        "title": "Summer 2027 ... Engineering Intern/Co-Op",
                        "posted_date": "2026-09-01T07:12:00+0000",
                        "full_location": "MARKHAM, Canada",
                        "apply_url": "https://campuscanada-amd.icims.com/jobs/90429/login",
                        "description": "..."}}]}

Notes:

* Each posting is wrapped: the fields live under ``jobs[i].data``.
* Ten postings per page; ``totalCount`` is the whole result, so pages are
  requested until it is reached. ``count`` is *not* the page size.
* The link sent to the user is the careers site's own job page,
  ``https://{host}{job_path}/{slug}`` (``/careers-home/jobs`` on AMD, ``/jobs``
  on SIG - verified live). The iCIMS ``apply_url`` is a login wall, so it is only
  a fallback.
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


@register
class JibeSource(JobSource):
    provider: ClassVar[str] = "jibe"
    required_config: ClassVar[tuple[str, ...]] = ("host",)

    @property
    def host(self) -> str:
        return self.config_str("host")

    @property
    def job_path(self) -> str:
        return str(self.config.get("job_path", "/jobs")).rstrip("/")

    @property
    def queries(self) -> tuple[str, ...]:
        return configured_queries(self.config, "keywords")

    def page_url(self, page: int, query: str | None = None) -> str:
        keywords = quote(query if query is not None else self.queries[0])
        return f"https://{self.host}/api/jobs?keywords={keywords}&page={page + 1}"

    @property
    def jobs_url(self) -> str:
        return self.page_url(0)

    def fetch_pages(self) -> Iterator[Sequence[Any]]:
        yield from self.search_all(self.queries, self._search)

    def _search(self, query: str) -> Iterator[Sequence[Any]]:
        page = 0
        seen = 0
        total: int | None = None
        while True:
            payload = self.client.get_json(self.page_url(page, query))
            if not isinstance(payload, dict):
                raise ParseError(
                    f"{self.describe()}: expected a JSON object, got {type(payload).__name__}"
                )
            jobs = payload.get("jobs")
            if not isinstance(jobs, list):
                raise ParseError(
                    f"{self.describe()}: page {page + 1} has no 'jobs' list "
                    f"(keys: {sorted(payload)[:8]})"
                )
            declared = payload.get("totalCount")
            if total is None and isinstance(declared, int) and declared > 0:
                total = declared
            if not jobs:
                return
            yield jobs
            seen += len(jobs)
            page += 1
            if total is not None and seen >= total:
                return

    def normalize(self, raw_job: Any) -> Job:
        wrapper = as_mapping(raw_job)
        record = as_mapping(wrapper.get("data")) or wrapper
        if not record:
            raise InvalidJobError(f"{self.describe()}: posting was not an object")

        slug = text_of(record, "slug", "req_id")
        if slug:
            url = f"https://{self.host}{self.job_path}/{slug}"
        else:
            url = text_of(record, "apply_url") or ""
        if not url:
            raise InvalidJobError(f"{self.describe()}: posting has neither slug nor apply url")

        return Job(
            company=self.company.company,
            title=text_of(record, "title") or "",
            url=url,
            source=self.provider,
            location=join_locations(
                [text_of(record, "full_location", "short_location", "location_name")]
            ),
            external_id=text_of(record, "req_id", "slug"),
            date_posted=record.get("posted_date") or record.get("create_date"),
            description=text_of(record, "description"),
            employment_type=text_of(record, "employment_type"),
        )


__all__: Sequence[str] = ("JibeSource",)
