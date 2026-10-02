"""New program pages in a published sitemap - Citadel and Citadel Securities.

Both firms' pages answer automated requests with 403, but the sitemaps they
publish for crawlers (robots.txt lists them) name every page under
``/careers/programs-and-events/``: one page per program (Discover Citadel, PhD
Summit, The Trading Invitational, Datathons) and one per application round
(``phd-summit/apply-for-citadel-phd-summit-london/``). A URL appearing there is a
new program or a newly opened round, so each URL is one item, titled from its
path. Nothing else is requested: the alert links to the page.

Config::

    {"provider": "sitemap_watch",
     "provider_config": {
        "url": "https://www.citadel.com/page-sitemap.xml",
        "path_prefix": "/careers/programs-and-events/"}}

The listing page itself (the prefix) is not an item. ``exclude_pattern`` (regex)
drops URLs that are not programs.

Captured live into ``tests/fixtures/events/``.
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterator, Sequence
from typing import Any, ClassVar
from urllib.parse import urlsplit

from jobmonitor.errors import InvalidJobError, ParseError
from jobmonitor.models.job import Job
from jobmonitor.scrapers._parse import as_mapping
from jobmonitor.scrapers.base import JobSource, register
from jobmonitor.scrapers.events._common import event_id, slug_title

_LOC = re.compile(r"<loc>\s*(?P<loc>[^<]+?)\s*</loc>")


def sitemap_urls(xml: str) -> list[str]:
    if "<urlset" not in xml[:2000]:
        raise ParseError("not a sitemap <urlset>")
    return [html.unescape(match.group("loc")) for match in _LOC.finditer(xml)]


def title_for(path: str, prefix: str) -> str:
    """A title from the path under ``prefix``.

    ``phd-summit/apply-for-citadel-phd-summit-london/`` becomes
    ``PhD Summit · Apply for Citadel PhD Summit London``.
    """
    rest = path[len(prefix) :].strip("/")
    return " · ".join(slug_title(segment) for segment in rest.split("/") if segment)


@register
class SitemapWatchSource(JobSource):
    provider: ClassVar[str] = "sitemap_watch"
    required_config: ClassVar[tuple[str, ...]] = ("url", "path_prefix")

    @property
    def prefix(self) -> str:
        prefix = self.config_str("path_prefix")
        return prefix if prefix.endswith("/") else prefix + "/"

    def fetch_pages(self) -> Iterator[Sequence[Any]]:
        xml = self.client.request(self.config_str("url")).text()
        try:
            urls = sitemap_urls(xml)
        except ParseError as exc:
            raise ParseError(f"{self.describe()}: {exc}") from exc
        exclude = self.config.get("exclude_pattern")
        records = []
        for url in urls:
            path = urlsplit(url).path
            if not path.startswith(self.prefix) or path.rstrip("/") == self.prefix.rstrip("/"):
                continue
            if exclude and re.search(str(exclude), url):
                continue
            records.append({"url": url, "title": title_for(path, self.prefix)})
        yield records

    def normalize(self, raw_job: Any) -> Job:
        record = as_mapping(raw_job)
        if not record.get("title"):
            raise InvalidJobError(f"{self.describe()}: sitemap URL without a path to title")
        return Job(
            company=self.company.company,
            title=str(record["title"]),
            url=str(record.get("url") or ""),
            source=self.provider,
            location=None,
            external_id=event_id(str(record.get("url"))),
            date_posted=None,
            description=None,
            # A program page or an application round, never a one-off talk.
            employment_type="Program",
        )


__all__: Sequence[str] = ("SitemapWatchSource", "sitemap_urls", "title_for")
