"""Public Luma calendars (``luma.com/<calendar>``).

Several companies run their meetups, hackathons and community events on Luma
(Databricks DevConnect, Claude community events, Vercel, Brex, NVIDIA, Together
AI). A calendar page is server-rendered by Next.js and embeds its upcoming events
as JSON (``__NEXT_DATA__`` → ``initialData.data.upcoming.entries``), so one
request reads them. robots.txt allows both the page and ``api.lu.ma``.

When the page says there are more upcoming events than it embeds (``has_more``),
the rest come from the public JSON endpoint the page itself uses::

    GET https://api.lu.ma/calendar/get-items?calendar_api_id=cal-...&period=future
        &pagination_limit=50[&pagination_cursor=...]

Config::

    {"provider": "luma", "provider_config": {"calendar": "databricks"}}

Each entry carries the event's name, start time and time zone, its page slug and
a structured place (``city_state``, ``country``). Online events say so.
Past events (already ended) are skipped.

Captured live into ``tests/fixtures/events/``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import datetime
from typing import Any, ClassVar
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jobmonitor.errors import InvalidJobError, ParseError
from jobmonitor.filtering.location import Region, classify_location
from jobmonitor.models.health import utcnow
from jobmonitor.models.job import Job
from jobmonitor.scrapers._parse import as_mapping, as_sequence
from jobmonitor.scrapers.base import MAX_PAGES, JobSource, register
from jobmonitor.scrapers.events._common import event_id, format_day

PAGE_URL = "https://luma.com/{calendar}"
API_URL = (
    "https://api.lu.ma/calendar/get-items?calendar_api_id={calendar_id}"
    "&period=future&pagination_limit=50"
)
_NEXT_DATA = re.compile(r"<script id=\"__NEXT_DATA__\"[^>]*>(?P<json>.*?)</script>", re.S)


def page_data(page: str) -> Mapping[str, Any]:
    """The ``initialData.data`` object a calendar page embeds."""
    match = _NEXT_DATA.search(page)
    if not match:
        raise ParseError("Luma page has no __NEXT_DATA__ payload")
    try:
        payload = json.loads(match.group("json"))
    except json.JSONDecodeError as exc:
        raise ParseError(f"Luma page payload is not JSON: {exc}") from exc
    initial = as_mapping(
        as_mapping(as_mapping(payload.get("props")).get("pageProps")).get("initialData")
    )
    if initial.get("kind") != "calendar":
        raise ParseError(f"not a Luma calendar page (kind={initial.get('kind')!r})")
    return as_mapping(initial.get("data"))


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _local(when: datetime, timezone: Any) -> datetime:
    try:
        return when.astimezone(ZoneInfo(str(timezone))) if timezone else when
    except (ZoneInfoNotFoundError, ValueError):
        return when


@register
class LumaSource(JobSource):
    provider: ClassVar[str] = "luma"
    required_config: ClassVar[tuple[str, ...]] = ("calendar",)

    #: Replaced in tests.
    clock: Callable[[], datetime] = staticmethod(utcnow)

    @property
    def calendar(self) -> str:
        return self.config_str("calendar").strip("/")

    @property
    def page_url(self) -> str:
        return PAGE_URL.format(calendar=quote(self.calendar))

    def fetch_pages(self) -> Iterator[Sequence[Any]]:
        data = page_data(self.client.request(self.page_url).text())
        upcoming = as_mapping(data.get("upcoming"))
        yield self._current(as_sequence(upcoming.get("entries")))
        if not upcoming.get("has_more"):
            return
        # The page embeds only the first few; the rest come from the API it uses.
        calendar_id = as_mapping(data.get("calendar")).get("api_id")
        if not calendar_id:
            raise ParseError(f"{self.describe()}: has_more but no calendar api_id")
        base = API_URL.format(calendar_id=quote(str(calendar_id)))
        url = base
        for _ in range(MAX_PAGES):
            payload = as_mapping(self.client.get_json(url))
            entries = as_sequence(payload.get("entries"))
            yield self._current(entries)
            cursor = payload.get("next_cursor")
            if not payload.get("has_more") or not cursor or not entries:
                return
            url = f"{base}&pagination_cursor={quote(str(cursor))}"

    def _current(self, entries: Sequence[Any]) -> list[Any]:
        """Entries not yet over. A past event is not malformed, just finished."""
        now = self.clock()
        current = []
        for entry in entries:
            event = as_mapping(as_mapping(entry).get("event")) or as_mapping(entry)
            end = _parse_time(event.get("end_at")) or _parse_time(event.get("start_at"))
            if end is None or end >= now:
                current.append(entry)
        return current

    def normalize(self, raw_job: Any) -> Job:
        entry = as_mapping(raw_job)
        event = as_mapping(entry.get("event")) or entry
        name = event.get("name")
        slug = event.get("url")
        identity = event.get("api_id") or entry.get("api_id")
        if not name or not slug or not identity:
            raise InvalidJobError(f"{self.describe()}: Luma entry without name/url/id")
        start = _parse_time(event.get("start_at") or entry.get("start_at"))
        place = as_mapping(event.get("geo_address_info"))
        if event.get("location_type") == "online":
            location: str | None = "Online"
        else:
            city = str(place.get("city_state") or place.get("city") or "")
            country = str(place.get("country") or "")
            if country and country.lower() in city.lower():
                country = ""  # "Bengaluru, India" already says it
            location = ", ".join(v for v in (city, country) if v) or None
        if location is None and classify_location(str(name)) is Region.NON_US:
            location = str(name)  # "Claw Agent Challenge: Paris": outside the US
        title = str(name)
        if start is not None:
            title = f"{title} · {format_day(_local(start, event.get('timezone')).date())}"
        return Job(
            company=self.company.company,
            title=title,
            url=f"https://luma.com/{slug}",
            source=self.provider,
            location=location,
            external_id=event_id(str(identity)),
            date_posted=None,
            description=None,
            employment_type=None,
        )


__all__: Sequence[str] = ("LumaSource", "page_data")
