"""A company's server-rendered events or programs page.

One configurable adapter for every such page instead of one scraper per site
(PRD §6). Pages differ in markup but share a shape: a list of cards, each a link
to the event with its name and, usually, a date. Configured per company in the
registry::

    {"provider": "event_page",
     "provider_config": {
        "url": "https://www.janestreet.com/join-jane-street/programs-and-events/",
        "link_pattern": "/programs-and-events/[a-z0-9-]+/?$"}}

Every ``<a href>`` whose absolute URL matches ``link_pattern`` (and not
``exclude_link_pattern``) is one event; several links to the same URL (image +
heading + "Learn more") are one event. Its title is the first of:

1. a heading or ``*title*``/``*name*``-classed element inside the link (the most
   prominent heading, so a card's ``<h3>`` name beats its ``<h4>PROGRAM`` label);
2. the link's own text, unless generic ("Learn more", "Register", "More info");
3. the last title-classed element or heading in the card before the link;
4. the URL's last path segment.

A title that is a whole card's text is cut back to the name before its date.
The event's date is the date nearest the link inside its card (the stretch of
page between the previous and the next event link), or the card's first date
before the link with ``"date_position": "before"``. Events whose date has passed
are skipped. A place named in the card ("San Francisco, CA", "Online") becomes
the location; failing that, a title naming a place outside the US ("World Tour
London") marks the event as outside the US, so the US filter drops it.

``require_date`` keeps only items with a date - on a busy marketing page, what
separates event cards from navigation. ``exclude_titles`` (regex) drops items a
link pattern cannot tell apart from events ("On demand" recordings). ``location`` sets one location
for every item, for pages that only list events in one place.

Captured live into ``tests/fixtures/events/``.
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, ClassVar
from urllib.parse import urldefrag, urljoin

from jobmonitor.errors import InvalidJobError, ParseError, ProviderConfigError
from jobmonitor.filtering.location import Region, classify_location
from jobmonitor.models.health import utcnow
from jobmonitor.models.job import Job
from jobmonitor.scrapers._parse import as_mapping
from jobmonitor.scrapers.base import JobSource, register
from jobmonitor.scrapers.events._common import (
    event_id,
    find_date,
    is_past,
    slug_title,
    text_of,
    trim_date,
    with_date,
)

_ANCHOR = re.compile(r"<a\b(?P<attrs>[^>]*)>(?P<inner>.*?)</a\s*>", re.I | re.S)
_HREF = re.compile(r"""\bhref\s*=\s*(?:"(?P<dq>[^"]*)"|'(?P<sq>[^']*)')""", re.I)
_ARIA = re.compile(r"""\baria-label\s*=\s*(?:"(?P<dq>[^"]*)"|'(?P<sq>[^']*)')""", re.I)
_HEADING = re.compile(r"<h(?P<level>[1-6])\b[^>]*>(?P<inner>.*?)</h(?P=level)\s*>", re.I | re.S)
#: An element whose class (or list-field attribute, as Webflow writes) names it a
#: title: ``class="program-title"``, ``fs-list-field="title"``.
_TITLED = re.compile(
    r"<(?P<tag>[a-z0-9]+)\b[^>]*(?:class|fs-list-field|data-field|itemprop)\s*=\s*"
    r"[\"'][^\"']*(?:title|heading|name)[^\"']*[\"'][^>]*>(?P<inner>.*?)</(?P=tag)\s*>",
    re.I | re.S,
)
_NOISE = re.compile(r"<(script|style|noscript|svg|template)\b.*?</\1\s*>", re.I | re.S)
#: Link texts that say what to do, not what the event is: "Learn more about this
#: event", "Register now", "More info". Only short texts count (see _is_generic).
_GENERIC = re.compile(
    r"^(?:learn more|more info(?:rmation)?|read more|see more|view|details|register|rsvp|"
    r"sign up|apply|find out more|explore|watch|join|save (?:my|your)|get tickets|browse|"
    r"more|here|link|event page|website|go to|click here|book|reserve)\b",
    re.I,
)
#: Card labels and section names that are not event names: "PROGRAM", "Upcoming Events".
_LABEL = re.compile(
    r"^(?:(?:upcoming|past|all|featured|recent|more|our|other)\s+)?"
    r"(?:programs?|events?|webinars?|conferences?|upcoming|featured|in[- ]person|virtual|"
    r"online|on[- ]demand|new|past|live|workshops?|meetups?|summits?|startup|enterprise)$",
    re.I,
)
#: Longest title kept from link text before it is treated as a whole card's text.
MAX_TITLE_CHARS = 140
#: How far before a link to look for its heading when the link text is generic.
LOOKBACK_CHARS = 4000
_SPLIT_PLACES = re.compile(r"\s*(?:\u00b7|\||\u2022|\u2014|\u2013|\s-\s|\n|\s{2,})\s*")


_MONTH_DAY = re.compile(
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s*\.?\s*\d", re.I
)


def _is_generic(text: str) -> bool:
    return (len(text) <= 40 and bool(_GENERIC.match(text))) or bool(_LABEL.match(text))


def _attr(pattern: re.Pattern[str], attrs: str) -> str | None:
    match = pattern.search(attrs)
    if not match:
        return None
    return html.unescape(match.group("dq") if match.group("dq") is not None else match.group("sq"))


@dataclass(slots=True)
class _Item:
    url: str
    start: int
    end: int
    titles: list[str] = field(default_factory=list)
    inner_text: str = ""


@register
class EventPageSource(JobSource):
    provider: ClassVar[str] = "event_page"
    required_config: ClassVar[tuple[str, ...]] = ("url", "link_pattern")

    #: Replaced in tests, so "past" is deterministic.
    clock: Callable[[], datetime] = staticmethod(utcnow)

    def _validate_config(self) -> None:
        super()._validate_config()
        for key in ("link_pattern", "exclude_link_pattern", "exclude_titles"):
            if self.config.get(key):
                try:
                    re.compile(str(self.config[key]))
                except re.error as exc:
                    raise ProviderConfigError(f"{self.describe()}: bad {key}: {exc}") from exc

    # ------------------------------------------------------------------ config
    @property
    def page_url(self) -> str:
        return self.config_str("url")

    def _pattern(self, key: str) -> re.Pattern[str] | None:
        value = self.config.get(key)
        return re.compile(str(value), re.I) if value else None

    # ------------------------------------------------------------------- fetch
    def fetch_pages(self) -> Iterator[Sequence[Any]]:
        response = self.client.request(self.page_url)
        page = response.text()
        if "<" not in page[:2000]:
            raise ParseError(f"{self.describe()}: {self.page_url} did not return HTML")
        yield self.parse(page)

    def parse(self, page: str) -> list[dict[str, Any]]:
        cleaned = _NOISE.sub(lambda m: " " * len(m.group(0)), page)
        today = self.clock().date()
        records = self._parse_links(cleaned, today)
        exclude = self._pattern("exclude_titles")
        require_date = bool(self.config.get("require_date"))
        kept = []
        for record in records:
            if exclude and exclude.search(record["title"]):
                continue
            if require_date and record.get("date") is None:
                continue
            if is_past(record.get("date"), now=self.clock()):
                continue
            kept.append(record)
        return kept

    def _parse_links(self, page: str, today: date) -> list[dict[str, Any]]:
        include = self._pattern("link_pattern")
        exclude = self._pattern("exclude_link_pattern")
        assert include is not None
        own = urldefrag(self.page_url).url.rstrip("/")
        items: dict[str, _Item] = {}
        for match in _ANCHOR.finditer(page):
            href = _attr(_HREF, match.group("attrs"))
            if not href or href.startswith(("javascript:", "mailto:", "tel:")):
                continue
            url = urldefrag(urljoin(self.page_url, href.strip())).url
            if url.rstrip("/") == own or not include.search(url):
                continue
            if exclude and exclude.search(url):
                continue
            item = items.get(url)
            if item is None:
                item = items[url] = _Item(url=url, start=match.start(), end=match.end())
            item.end = match.end()
            title = self._title_in(match.group("inner"), match.group("attrs"))
            if title:
                item.titles.append(title)
            inner = text_of(match.group("inner"))
            if len(inner) > len(item.inner_text):
                item.inner_text = inner

        ordered = sorted(items.values(), key=lambda item: item.start)
        records = []
        for index, item in enumerate(ordered):
            card_start = ordered[index - 1].end if index else max(0, item.start - LOOKBACK_CHARS)
            card_end = ordered[index + 1].start if index + 1 < len(ordered) else item.end + 2000
            before = page[card_start : item.start]
            after = page[item.end : card_end]
            title = next((t for t in item.titles if not _is_generic(t)), None)
            if title is None:
                # The name sits in the card before a "Learn more" link.
                title = self._title_before(before) or self._heading_before(
                    page[max(0, item.start - LOOKBACK_CHARS) : item.start]
                )
            if title is None:
                title = slug_title(item.url)
            records.append(
                self._record(
                    title=title,
                    url=item.url,
                    identity=item.url,
                    inner=item.inner_text,
                    before=text_of(before),
                    after=text_of(after),
                    today=today,
                )
            )
        return records

    def _record(
        self,
        *,
        title: str,
        url: str,
        identity: str,
        inner: str,
        before: str,
        after: str,
        today: date,
    ) -> dict[str, Any]:
        found = (
            find_date(title, today=today)
            or find_date(inner, today=today)
            or self._card_date(before, after, today=today)
        )
        when, raw = found if found else (None, None)
        if found and raw and raw in title:
            title = trim_date(title, today=today)
            raw = None
        location = (
            self.config.get("location")
            or _location_in(inner, title)
            or _location_in(after[:300], title)
        )
        if location is None and classify_location(title) is Region.NON_US:
            # "AIforce World Tour London", "Claude Founder House Stockholm": the
            # name says where, and it is outside the US, so the US filter drops it.
            location = title
        description = inner if inner and inner != title else (after[:500] or None)
        return {
            "id": identity,
            "title": with_date(title, when, raw),
            "url": url,
            "date": when,
            "location": location,
            "description": description,
        }

    # ----------------------------------------------------------------- helpers
    def _card_date(self, before: str, after: str, *, today: date) -> tuple[date, str] | None:
        """The card's date. ``date_position`` says where cards put it relative to
        their link: ``before`` (Pinterest: name, date, then "Learn more"),
        ``after``, or ``nearest`` (default)."""
        position = str(self.config.get("date_position") or "nearest")
        if position == "before":
            # ``before`` starts where the previous card's link ended: its first
            # date is this card's (start) date.
            return find_date(before, today=today)
        if position == "after":
            return find_date(after[:400], today=today)
        return _nearest_date(before, after, today=today)

    @staticmethod
    def _title_in(inner: str, attrs: str) -> str | None:
        candidates = [text_of(m.group("inner")) for m in _TITLED.finditer(inner)]
        # Then headings, most prominent first: a card's <h3> name beats its <h4>
        # "PROGRAM" label and <h6> status line.
        headings = sorted(_HEADING.finditer(inner), key=lambda m: m.group("level"))
        candidates += [text_of(m.group("inner")) for m in headings]
        for title in candidates:
            if title and not _is_generic(title):
                return title[:MAX_TITLE_CHARS]
        text = text_of(inner)
        if text and len(text) <= MAX_TITLE_CHARS:
            return text
        aria = _attr(_ARIA, attrs)
        if aria and not _is_generic(aria):
            return text_of(aria)[:MAX_TITLE_CHARS]
        return None

    @staticmethod
    def _title_before(card: str) -> str | None:
        for match in reversed(list(_TITLED.finditer(card))):
            title = text_of(match.group("inner"))
            if title and not _is_generic(title):
                return title[:MAX_TITLE_CHARS]
        return None

    @staticmethod
    def _heading_before(fragment: str) -> str | None:
        headings = list(_HEADING.finditer(fragment))
        for match in reversed(headings):
            title = text_of(match.group("inner"))
            if title and not _is_generic(title):
                return title[:MAX_TITLE_CHARS]
        return None

    # --------------------------------------------------------------- normalize
    def normalize(self, raw_job: Any) -> Job:
        record = as_mapping(raw_job)
        if not record.get("title"):
            raise InvalidJobError(f"{self.describe()}: event without a title")
        return Job(
            company=self.company.company,
            title=str(record["title"]),
            url=str(record.get("url") or ""),
            source=self.provider,
            location=record.get("location"),
            external_id=event_id(str(record.get("id") or record.get("url"))),
            date_posted=None,
            description=record.get("description"),
            employment_type=None,  # the pipeline labels it by the source's category
        )


def _last_date(before: str, *, today: date) -> tuple[date, str] | None:
    """The last date in ``before`` (its final 400 characters)."""
    tail = before[-400:]
    # find_date returns the first match; walk back to find the last one.
    for offset in range(len(tail) - 1, -1, -40):
        hit = find_date(tail[offset:], today=today)
        if hit:
            return hit
    return None


def _nearest_date(before: str, after: str, *, today: date) -> tuple[date, str] | None:
    """The date closest to the link: the end of ``before`` or the start of ``after``."""
    after_hit = find_date(after[:400], today=today)
    before_hit = _last_date(before, today=today)
    tail = before[-400:]
    if after_hit and before_hit:
        after_distance = after.find(after_hit[1])
        before_distance = len(tail) - tail.rfind(before_hit[1])
        return after_hit if after_distance <= before_distance else before_hit
    return after_hit or before_hit


def _location_in(text: str, title: str) -> str | None:
    """A place named in a card's own text ("New York, NY", "London"), if any.

    Pieces of the title are skipped: "The Optiver & Oxford Trading Academy" is a
    name, not a location.
    """
    for piece in _SPLIT_PLACES.split(text or ""):
        if not piece or len(piece) > 40 or find_date(piece, today=date(2000, 1, 1)):
            continue
        if piece.lower() in title.lower():
            continue
        if re.search(r"\b20\d\d\b|\d{1,2}:\d\d", piece) or _MONTH_DAY.search(piece):
            continue  # a fragment of a date or time, not a place
        if re.search(r"\b(?:virtual|online|remote|webinar|on[- ]demand)\b", piece, re.I):
            return "Online"
        if classify_location(piece) is not Region.UNKNOWN:
            return piece
    return None


__all__: Sequence[str] = ("EventPageSource",)
