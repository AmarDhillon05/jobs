"""Avature career portals - Bloomberg.

bloomberg.com answers automated requests with 403, but Bloomberg's job board
is hosted on Avature (``bloomberg.avature.net``), whose robots.txt explicitly
allows ``/careers`` and publishes a sitemap of every open job. Reading what the
site publishes for crawlers is the polite path, and cheap:

1. ``GET https://{host}/{portal}/sitemap.xml`` - **one request** listing every
   open posting as ``.../JobDetail/<Title-Slug>/<id>``, with a ``lastmod``.
2. Only for slugs whose words name an internship (``intern``, ``internship``,
   ``co-op``, ...; "Internal" and "International" do not count), ``GET`` the
   detail page for its title, location and description. Detail requests are
   paced (``detail_delay_seconds``, default 1 s): Avature reset connections
   when pages were requested back to back during research.

Why not the portal's other endpoints, all tried on 2026-10-01:

* the RSS feed (``/SearchJobs/feed/``) ignores the search term and stops at 20;
* the HTML search caps a page at 12, and ``intern`` matches ~200 postings
  (it is a substring search: "Internal", "International"), so 17 requests a
  poll instead of 1 + however many internships are open.

Postings are treated as undated: ``lastmod`` is a modification date, not a
posting date, and ``first_seen`` is authoritative anyway (PRD §9).

Captured live into ``tests/fixtures/avature/``.
"""

from __future__ import annotations

import html
import re
import time
from collections.abc import Callable, Iterator, Sequence
from itertools import pairwise
from typing import Any, ClassVar

from jobmonitor.errors import InvalidJobError, JobMonitorError, ParseError
from jobmonitor.models.job import Job
from jobmonitor.scrapers._parse import as_mapping, as_sequence
from jobmonitor.scrapers.base import JobSource, register

#: Slug words that mark an internship. Matched as whole words of the slug.
DEFAULT_TITLE_WORDS = ("intern", "interns", "internship", "internships", "co-op", "coop")

_URL = re.compile(r"<url>(?P<body>.*?)</url>", re.S)
_LOC = re.compile(r"<loc>\s*(?P<loc>[^<]+?)\s*</loc>")
_LASTMOD = re.compile(r"<lastmod>\s*(?P<lastmod>[^<]+?)\s*</lastmod>")
_JOB_PATH = re.compile(r"/JobDetail/(?P<slug>[^/?#]+)/(?P<id>\d+)")
_TITLE = re.compile(r"<title>(?P<title>.*?)</title>", re.S)
_TITLE_SUFFIX = re.compile(r"\s+-\s+\d+\s+-\s+[^-]+$")
_FIELD = re.compile(
    r"article__content__view__field__label[^>]*>\s*(?P<label>[^<]+?)\s*</div>\s*"
    r"<div[^>]*article__content__view__field__value[^>]*>(?P<value>.*?)</div>",
    re.S,
)
_DESCRIPTION = re.compile(
    r"Description\s*(?:&amp;|&)\s*Requirements\s*</h\d>(?P<body>.*?)(?:<h\d|$)", re.S
)


def _text(fragment: str) -> str:
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", fragment))).strip()


def parse_sitemap(xml: str) -> list[dict[str, str]]:
    """Every job posting in a portal sitemap: id, slug, url, lastmod."""
    postings: list[dict[str, str]] = []
    for block in _URL.finditer(xml):
        body = block.group("body")
        loc = _LOC.search(body)
        if not loc:
            continue
        url = html.unescape(loc.group("loc"))
        path = _JOB_PATH.search(url)
        if not path:
            continue  # portal pages, not postings
        lastmod = _LASTMOD.search(body)
        postings.append(
            {
                "id": path.group("id"),
                "slug": path.group("slug"),
                "url": url,
                "lastmod": lastmod.group("lastmod") if lastmod else "",
            }
        )
    return postings


def slug_words(slug: str) -> set[str]:
    words = slug.lower().split("-")
    # "Co-op" arrives as two slug words; rejoin it so it can be matched.
    joined = {f"{a}-{b}" for a, b in pairwise(words)}
    return set(words) | joined


def parse_detail(page: str) -> dict[str, str | None]:
    """Title, labelled fields and a description preview from a JobDetail page."""
    title_match = _TITLE.search(page)
    # "<title> Internal Communications Specialist - 44960 - Bloomberg </title>": drop
    # the "- <id> - <company>" suffix only, so a title may itself contain " - ".
    raw = _text(title_match.group("title")) if title_match else ""
    title = _TITLE_SUFFIX.sub("", raw).strip()
    fields = {_text(m.group("label")): _text(m.group("value")) for m in _FIELD.finditer(page)}
    description = _DESCRIPTION.search(page)
    preview = _text(description.group("body")) if description else ""
    return {
        "title": title or None,
        "location": fields.get("Location") or None,
        "reference": fields.get("Ref #") or None,
        "business_area": fields.get("Business Area") or None,
        "description": preview[:2000] or None,
    }


@register
class AvatureSource(JobSource):
    provider: ClassVar[str] = "avature"
    required_config: ClassVar[tuple[str, ...]] = ("host",)

    #: Pause between detail-page requests. Replaced in tests.
    sleep: Callable[[float], None] = staticmethod(time.sleep)

    @property
    def host(self) -> str:
        return self.config_str("host")

    @property
    def portal(self) -> str:
        return str(self.config.get("portal") or "careers").strip("/")

    @property
    def title_words(self) -> frozenset[str]:
        words = as_sequence(self.config.get("title_words")) or DEFAULT_TITLE_WORDS
        return frozenset(str(word).lower() for word in words)

    @property
    def detail_delay(self) -> float:
        return float(self.config.get("detail_delay_seconds", 1.0))

    @property
    def jobs_url(self) -> str:
        return f"https://{self.host}/{self.portal}/sitemap.xml"

    def wanted(self, posting: dict[str, str]) -> bool:
        return bool(slug_words(posting["slug"]) & self.title_words)

    def fetch_pages(self) -> Iterator[Sequence[Any]]:
        sitemap = self.client.request(self.jobs_url).text()
        if "<urlset" not in sitemap:
            raise ParseError(f"{self.describe()}: {self.jobs_url} is not a sitemap")
        postings = [p for p in parse_sitemap(sitemap) if self.wanted(p)]
        records: list[dict[str, Any]] = []
        for index, posting in enumerate(postings):
            if index and self.detail_delay > 0:
                self.sleep(self.detail_delay)
            record: dict[str, Any] = dict(posting)
            try:
                record.update(parse_detail(self.client.request(posting["url"]).text()))
            except JobMonitorError as exc:
                # One unreadable posting is that posting's problem, not the board's:
                # it is counted as malformed and retried on the next poll.
                record["error"] = f"{type(exc).__name__}: {exc}"
            records.append(record)
        yield records

    def normalize(self, raw_job: Any) -> Job:
        record = as_mapping(raw_job)
        if record.get("error"):
            raise InvalidJobError(f"{self.describe()}: {record.get('url')}: {record['error']}")
        title = record.get("title") or str(record.get("slug") or "").replace("-", " ")
        return Job(
            company=self.company.company,
            title=str(title),
            url=str(record.get("url") or ""),
            source=self.provider,
            location=record.get("location"),
            external_id=record.get("id"),
            date_posted=None,
            description=record.get("description"),
            employment_type="Internship",
        )


__all__: Sequence[str] = (
    "DEFAULT_TITLE_WORDS",
    "AvatureSource",
    "parse_detail",
    "parse_sitemap",
    "slug_words",
)
