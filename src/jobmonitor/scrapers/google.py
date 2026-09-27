"""Google careers (google.com/about/careers).

There is no public JSON API. The results page is server-rendered, and it embeds
its own data in the HTML as an ``AF_initDataCallback`` block keyed ``ds:1`` - a
plain GET returns it, no script execution needed (PRD §5 preference 5,
server-rendered HTML):

    GET https://www.google.com/about/careers/applications/jobs/results?q={query}&page={n}

The embedded payload, captured live on 2026-09-27 and trimmed into
``tests/fixtures/google/results_page*.html``, is positional::

    [[job, job, ...],   # 20 per page
     null,
     61,                # total matches
     20]                # page size

    job[0]   "80582381009806022"                 id
    job[1]   "Technical Program Manager Intern"  title
    job[9]   [["Mountain View, CA, USA", ...], ...]  locations
    job[10]  [null, "<p>...</p>"]                description (HTML)
    job[13]  [1790100436, 985000000]             published (epoch s, nanos)

Notes:

* **Which timestamp is the posting date was established, not assumed.** Each job
  carries three: [12], [13] and [14]. Sorting Google's own results by date
  follows [13]; [12] is shared by dozens of jobs from one batch import on the same
  second. So [13] is used.
* Positional data is inherently fragile - a Google redesign can move a field.
  The adapter therefore finds the jobs block by its *shape* rather than trusting
  the ``ds:1`` key, validates each record, and raises a ``ParseError`` naming the
  problem rather than emitting half-parsed jobs. The live validation run is what
  catches a redesign.
* The public job page is ``/jobs/results/{id}`` (verified live).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from typing import Any, ClassVar
from urllib.parse import quote

from jobmonitor.errors import InvalidJobError, ParseError
from jobmonitor.models.job import Job
from jobmonitor.scrapers._parse import join_locations
from jobmonitor.scrapers._search import configured_queries
from jobmonitor.scrapers.base import JobSource, register

SITE = "https://www.google.com/about/careers/applications/jobs/results"

_CALLBACK = re.compile(
    r"AF_initDataCallback\(\{key: '(?P<key>ds:\d+)'.*?data:(?P<data>.*?), sideChannel: \{\}\}\);",
    re.S,
)

# Positions within one job record (see the module docstring).
_ID, _TITLE, _LOCATIONS, _DESCRIPTION, _PUBLISHED = 0, 1, 9, 10, 13


def _looks_like_jobs_block(data: Any) -> bool:
    """``[[ [id, title, ...], ... ], _, total, page_size]``."""
    if not (isinstance(data, list) and len(data) >= 3 and isinstance(data[0], list)):
        return False
    first = data[0][0] if data[0] else None
    return first is None or (
        isinstance(first, list)
        and len(first) > _PUBLISHED
        and isinstance(first[_ID], str)
        and isinstance(first[_TITLE], str)
    )


def extract_jobs_block(html: str) -> list[Any]:
    """The embedded ``[jobs, _, total, page_size]`` payload from a results page."""
    for match in _CALLBACK.finditer(html):
        try:
            data = json.loads(match.group("data"))
        except ValueError:
            continue
        if _looks_like_jobs_block(data):
            return data
    raise ParseError("no embedded jobs block (AF_initDataCallback) found in the results page")


@register
class GoogleCareersSource(JobSource):
    provider: ClassVar[str] = "google"
    required_config: ClassVar[tuple[str, ...]] = ()

    @property
    def queries(self) -> tuple[str, ...]:
        return configured_queries(self.config, "query")

    def page_url(self, page: int, query: str | None = None) -> str:
        term = quote(query if query is not None else self.queries[0])
        return f"{SITE}?q={term}&page={page + 1}"

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
            response = self.client.request(
                self.page_url(page, query), headers={"Accept": "text/html"}
            )
            try:
                block = extract_jobs_block(response.text())
            except ParseError as exc:
                raise ParseError(f"{self.describe()}: page {page + 1}: {exc}") from exc
            jobs = block[0] or []
            declared = block[2] if len(block) > 2 else None
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
        if not isinstance(raw_job, list) or len(raw_job) <= _TITLE:
            raise InvalidJobError(f"{self.describe()}: job record was not a positional list")
        identifier = raw_job[_ID]
        title = raw_job[_TITLE]
        if not isinstance(identifier, str) or not identifier:
            raise InvalidJobError(f"{self.describe()}: job record has no id")

        return Job(
            company=self.company.company,
            title=title if isinstance(title, str) else "",
            url=f"{SITE}/{identifier}",
            source=self.provider,
            location=join_locations(_locations(raw_job)),
            external_id=identifier,
            date_posted=_published(raw_job),
            description=_html(raw_job, _DESCRIPTION),
            employment_type=None,
        )


def _field(record: list[Any], index: int) -> Any:
    return record[index] if len(record) > index else None


def _locations(record: list[Any]) -> list[str | None]:
    places = _field(record, _LOCATIONS)
    if not isinstance(places, list):
        return []
    return [
        place[0] if isinstance(place, list) and place and isinstance(place[0], str) else None
        for place in places
    ]


def _published(record: list[Any]) -> datetime | None:
    stamp = _field(record, _PUBLISHED)
    if isinstance(stamp, list) and stamp and isinstance(stamp[0], int):
        return datetime.fromtimestamp(stamp[0], UTC)
    return None


def _html(record: list[Any], index: int) -> str | None:
    value = _field(record, index)
    if isinstance(value, list) and len(value) > 1 and isinstance(value[1], str):
        return value[1]
    return None


__all__: Sequence[str] = ("SITE", "GoogleCareersSource", "extract_jobs_block")
