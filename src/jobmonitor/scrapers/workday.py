"""Workday CXS adapter - 45 companies in the shipped registry. **Paginated, POST.**

Endpoint (the JSON API a Workday careers page calls from the browser, no
authentication):

    POST https://{host}/wday/cxs/{tenant}/{site}/jobs
    {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": "intern"}

Response shape encoded by ``tests/fixtures/workday/*.json``::

    {"total": 57,
     "jobPostings": [{"title": "Software Engineering Intern",
                      "externalPath": "/job/Santa-Clara/Software-Eng-Intern_JR1234",
                      "locationsText": "Santa Clara, CA",
                      "postedOn": "Posted 5 Days Ago",
                      "bulletFields": ["JR1234"]}]}

Three things make Workday the most awkward provider here, and all three are
handled deliberately:

* **``limit`` is capped at 20 server-side**, so a tenant with 300 postings needs
  15 requests. ``searchText="intern"`` narrows that to the roles we want; the
  filtering layer still makes the final call.
* **``postedOn`` is relative English text** ("Posted Today", "Posted 30+ Days
  Ago"), not a timestamp - :func:`parse_posted_on` converts it against an
  injected clock. This is exactly the "unreliable posting timestamps" case that
  makes ``first_seen`` authoritative (PRD §9).
* **The requisition ID lives in ``bulletFields``**, an untyped array. It is the
  only stable external id Workday offers, so it is extracted by pattern rather
  than position.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

from jobmonitor.errors import InvalidJobError, ParseError
from jobmonitor.models.job import Job
from jobmonitor.scrapers._parse import as_mapping, as_sequence, first_of, text, text_of
from jobmonitor.scrapers.base import JobSource, register

#: Workday caps this server-side; asking for more is silently ignored.
PAGE_SIZE = 20
DEFAULT_SEARCH_TEXT = "intern"

_DAYS_AGO_RE = re.compile(r"posted\s+(\d+)\+?\s*day", re.IGNORECASE)
_MONTHS_AGO_RE = re.compile(r"posted\s+(\d+)\+?\s*month", re.IGNORECASE)
#: Requisition ids: JR1234, R-1234, REQ36193, 12345678.
_REQ_ID_RE = re.compile(r"^(?:[A-Z]{1,5}[-_]?\d{3,}|\d{5,})$")


def parse_posted_on(value: str | None, *, now: datetime | None = None) -> datetime | None:
    """Convert Workday's relative ``postedOn`` text to a timestamp.

    >>> ref = datetime(2026, 9, 26, tzinfo=UTC)
    >>> parse_posted_on("Posted Today", now=ref).date().isoformat()
    '2026-09-26'
    >>> parse_posted_on("Posted 5 Days Ago", now=ref).date().isoformat()
    '2026-09-21'

    Returns ``None`` for anything it cannot read, which is fine: detection timing
    comes from ``first_seen``, not from the source's own claim.
    """
    if not value:
        return None
    # Truncated to the day: the source only has day granularity, and keeping the
    # sub-day part would make an identical posting look different every second.
    reference = (now or datetime.now(UTC)).replace(hour=0, minute=0, second=0, microsecond=0)
    lowered = value.strip().casefold()
    if "today" in lowered:
        return reference
    if "yesterday" in lowered:
        return reference - timedelta(days=1)
    match = _DAYS_AGO_RE.search(lowered)
    if match:
        return reference - timedelta(days=int(match.group(1)))
    match = _MONTHS_AGO_RE.search(lowered)
    if match:
        return reference - timedelta(days=30 * int(match.group(1)))
    return None


@register
class WorkdaySource(JobSource):
    provider: ClassVar[str] = "workday"
    required_config: ClassVar[tuple[str, ...]] = ("host", "tenant", "site")

    def __init__(self, *args: Any, clock: Callable[[], datetime] | None = None, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def host(self) -> str:
        return self.config_str("host")

    @property
    def tenant(self) -> str:
        return self.config_str("tenant")

    @property
    def site(self) -> str:
        return self.config_str("site")

    @property
    def search_text(self) -> str:
        return str(self.config.get("search_text", DEFAULT_SEARCH_TEXT))

    @property
    def jobs_url(self) -> str:
        return f"https://{self.host}/wday/cxs/{self.tenant}/{self.site}/jobs"

    @property
    def locale(self) -> str:
        return str(self.config.get("locale", "en-US"))

    def fetch_pages(self) -> Iterator[Sequence[Any]]:
        offset = 0
        seen = 0
        total: int | None = None

        while True:
            payload = self.client.post_json(
                self.jobs_url,
                {
                    "appliedFacets": {},
                    "limit": PAGE_SIZE,
                    "offset": offset,
                    "searchText": self.search_text,
                },
            )
            if not isinstance(payload, dict):
                raise ParseError(
                    f"{self.describe()}: expected a JSON object from {self.jobs_url}, "
                    f"got {type(payload).__name__}"
                )
            postings = payload.get("jobPostings")
            if postings is None:
                raise ParseError(
                    f"{self.describe()}: page at offset {offset} has no 'jobPostings' "
                    f"(keys: {sorted(payload)[:8]})"
                )
            if not isinstance(postings, list):
                raise ParseError(
                    f"{self.describe()}: 'jobPostings' was {type(postings).__name__}, expected list"
                )

            # Workday reports the real total on the FIRST page only; every later
            # page says `"total": 0`. Overwriting with that 0 made `seen >= total`
            # true after page two, silently truncating every tenant to 40 postings
            # (observed live: NVIDIA declares 1,010 and returned 40). So the first
            # positive total is kept and later values are ignored.
            declared = payload.get("total")
            if total is None and isinstance(declared, int) and declared > 0:
                total = declared

            if not postings:
                return
            yield postings

            seen += len(postings)
            offset += len(postings)
            if total is not None and seen >= total:
                return
            if len(postings) < PAGE_SIZE:
                return

    def normalize(self, raw_job: Any) -> Job:
        record = as_mapping(raw_job)
        if not record:
            raise InvalidJobError(f"{self.describe()}: posting was not an object")

        path = text_of(record, "externalPath")
        if not path:
            raise InvalidJobError(
                f"{self.describe()}: posting {record.get('title')!r} has no externalPath"
            )
        if not path.startswith("/"):
            path = f"/{path}"
        url = f"https://{self.host}/{self.locale}/{self.site}{path}"

        return Job(
            company=self.company.company,
            title=text_of(record, "title") or "",
            url=url,
            source=self.provider,
            location=text_of(record, "locationsText", "location"),
            external_id=self._requisition_id(record, path),
            date_posted=parse_posted_on(text_of(record, "postedOn"), now=self._clock()),
            description=text_of(record, "jobDescription", "shortDescription"),
            employment_type=first_of(record, "timeType", "jobType"),
        )

    @staticmethod
    def _requisition_id(record: Any, path: str) -> str | None:
        """Pull the requisition id out of ``bulletFields``, else off the URL path.

        ``bulletFields`` is an untyped array whose contents vary by tenant, so the
        id is found by shape rather than by index.
        """
        for field in as_sequence(as_mapping(record).get("bulletFields")):
            value = text(field)
            if value and _REQ_ID_RE.match(value):
                return value
        # Fallback: paths end in `..._JR1234`.
        tail = path.rsplit("_", 1)[-1] if "_" in path else ""
        return tail if tail and _REQ_ID_RE.match(tail) else None


__all__: Sequence[str] = (
    "DEFAULT_SEARCH_TEXT",
    "PAGE_SIZE",
    "WorkdaySource",
    "parse_posted_on",
)
