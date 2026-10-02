"""Avature events portals - Bloomberg and Two Sigma.

Both companies list recruiting events (info sessions, coffee chats, "Discover
Bloomberg" days) on the same Avature product that serves their jobs, under an
``events`` portal that robots.txt allows. The list page is server-rendered: one
``<article>`` per event with its title link (``EventDetail?jobId=<id>``), place,
format and date, so **one request per page** reads everything; no detail pages.

Config::

    {"provider": "avature_events",
     "provider_config": {"url": "https://bloomberg.avature.net/events/EventsList"}}

The list is paginated (Bloomberg shows 9 a page); the "next" link is followed
until there is none, paced by ``page_delay_seconds`` (default 1 s).

Dates arrive as ``05-Oct-2026`` (Two Sigma) or a ``Sep``/``08`` badge with no
year (Bloomberg, read as the occurrence nearest today). Past events are skipped.

Captured live into ``tests/fixtures/events/``.
"""

from __future__ import annotations

import html
import re
import time
from collections.abc import Callable, Iterator, Sequence
from datetime import date, datetime
from typing import Any, ClassVar
from urllib.parse import urljoin

from jobmonitor.errors import InvalidJobError, ParseError
from jobmonitor.models.health import utcnow
from jobmonitor.models.job import Job
from jobmonitor.scrapers._parse import as_mapping
from jobmonitor.scrapers.base import MAX_PAGES, JobSource, register
from jobmonitor.scrapers.events._common import event_id, find_date, is_past, text_of, with_date

_ARTICLE = re.compile(
    r"<article\b[^>]*class=\"[^\"]*\barticle--(?:result|card)\b[^>]*>(?P<body>.*?)</article>", re.S
)
_TITLE_LINK = re.compile(
    r"<h\d[^>]*__title[^>]*>\s*<a\b[^>]*href=\"(?P<href>[^\"]+)\"[^>]*>(?P<title>.*?)</a>", re.S
)
_JOB_ID = re.compile(r"[?&]jobId=(?P<id>\d+)")
_LOCATION = re.compile(
    r"class=\"[^\"]*(?:list-item-eventLocation|icon-as-bg--location)[^\"]*\"[^>]*>(?P<value>.*?)</span>",
    re.S,
)
_FORMAT = re.compile(
    r"class=\"[^\"]*(?:list-item-eventFormat|icon-as-bg--venue)[^\"]*\"[^>]*>(?P<value>.*?)</span>",
    re.S,
)
_BADGE = re.compile(
    r"date__day\"[^>]*>\s*(?P<month>[A-Za-z]{3,9})\s*</span>\s*<span[^>]*date__number\"[^>]*>"
    r"\s*(?P<day>\d{1,2})\s*<",
    re.S,
)
_DMY = re.compile(r"\b(?P<day>\d{1,2})-(?P<month>[A-Za-z]{3})-(?P<year>20\d\d)\b")
_NEXT = re.compile(
    r"<a\b[^>]*class=\"[^\"]*paginationNextLink[^\"]*\"[^>]*href=\"(?P<href>[^\"]+)\"", re.S
)
_NEXT_ALT = re.compile(
    r"<a\b[^>]*href=\"(?P<href>[^\"]+)\"[^>]*class=\"[^\"]*paginationNextLink", re.S
)


def _event_date(body: str, *, today: date) -> date | None:
    dmy = _DMY.search(body)
    if dmy:
        found = find_date(f"{dmy['month']} {dmy['day']}, {dmy['year']}", today=today)
        return found[0] if found else None
    badge = _BADGE.search(body)
    if badge:
        found = find_date(f"{badge['month']} {badge['day']}", today=today)
        return found[0] if found else None
    return None


def parse_events(page: str, *, base_url: str, today: date) -> list[dict[str, Any]]:
    """Every event article on one list page."""
    records = []
    for article in _ARTICLE.finditer(page):
        body = article.group("body")
        link = _TITLE_LINK.search(body)
        if not link:
            continue
        url = urljoin(base_url, html.unescape(link.group("href")))
        identity = _JOB_ID.search(url)
        location = _LOCATION.search(body)
        venue = _FORMAT.search(body)
        records.append(
            {
                "id": identity.group("id") if identity else url,
                "title": text_of(link.group("title")),
                "url": url,
                "date": _event_date(body, today=today),
                "location": text_of(location.group("value")) if location else None,
                "format": text_of(venue.group("value")) if venue else None,
            }
        )
    return records


def next_page(page: str, *, base_url: str) -> str | None:
    match = _NEXT.search(page) or _NEXT_ALT.search(page)
    return urljoin(base_url, html.unescape(match.group("href"))) if match else None


@register
class AvatureEventsSource(JobSource):
    provider: ClassVar[str] = "avature_events"
    required_config: ClassVar[tuple[str, ...]] = ("url",)

    #: Replaced in tests.
    clock: Callable[[], datetime] = staticmethod(utcnow)
    sleep: Callable[[float], None] = staticmethod(time.sleep)

    @property
    def page_delay(self) -> float:
        return float(self.config.get("page_delay_seconds", 1.0))

    def fetch_pages(self) -> Iterator[Sequence[Any]]:
        url: str | None = self.config_str("url")
        seen: set[str] = set()
        today = self.clock().date()
        while url and url not in seen and len(seen) < MAX_PAGES:
            if seen and self.page_delay > 0:
                self.sleep(self.page_delay)
            seen.add(url)
            page = self.client.request(url).text()
            if "<html" not in page[:5000].lower():
                raise ParseError(f"{self.describe()}: {url} did not return an HTML page")
            yield [
                record
                for record in parse_events(page, base_url=url, today=today)
                if not is_past(record["date"], now=self.clock())
            ]
            url = next_page(page, base_url=url)

    def normalize(self, raw_job: Any) -> Job:
        record = as_mapping(raw_job)
        if not record.get("title"):
            raise InvalidJobError(f"{self.describe()}: event without a title")
        details = " · ".join(str(v) for v in (record.get("format"), record.get("location")) if v)
        return Job(
            company=self.company.company,
            title=with_date(str(record["title"]), record.get("date")),
            url=str(record.get("url") or ""),
            source=self.provider,
            location=record.get("location"),
            external_id=event_id(str(record.get("id"))),
            date_posted=None,
            description=details or None,
            employment_type=None,
        )


__all__: Sequence[str] = ("AvatureEventsSource", "next_page", "parse_events")
