"""Custom scraper: schema.org ``JobPosting`` JSON-LD from a server-rendered page.

Many company-owned careers pages have no JSON API but do embed structured data
for Google Jobs:

    <script type="application/ld+json">
      {"@context":"https://schema.org","@type":"JobPosting",
       "title":"Software Engineer Intern","datePosted":"2026-09-01",
       "identifier":{"@type":"PropertyValue","value":"REQ-1"},
       "hiringOrganization":{"@type":"Organization","name":"Acme"},
       "jobLocation":{"@type":"Place","address":{"@type":"PostalAddress",
                      "addressLocality":"Austin","addressRegion":"TX"}},
       "employmentType":"INTERN","url":"https://acme.example.com/careers/1",
       "description":"<p>...</p>"}
    </script>

This is the PRD §5 "server-rendered HTML" tier, and it is written once as a
*configurable* scraper rather than per company, so adding a site is a registry
entry: ``{"provider": "json_ld", "provider_config": {"url": "...",
"base_url": "..."}}``.

Deliberate scope limits:

* Parsing uses :class:`html.parser.HTMLParser` from the standard library - no
  new runtime dependency, and the Lambda asset stays dependency-free.
* Only ``JobPosting`` objects are read, including those nested in ``@graph`` or
  in a top-level array.
* It reads what the page already serves to any browser. It does not execute
  JavaScript, follow login flows, or touch bot protection (PRD §5/§33).
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from html.parser import HTMLParser
from typing import Any, ClassVar
from urllib.parse import urljoin

from jobmonitor.errors import InvalidJobError, ParseError
from jobmonitor.models.job import Job
from jobmonitor.scrapers._parse import (
    as_mapping,
    as_sequence,
    compose_location,
    first_of,
    join_locations,
    text_of,
)
from jobmonitor.scrapers.base import JobSource, register

LD_JSON_TYPE = "application/ld+json"


class _LdJsonCollector(HTMLParser):
    """Collects the text of every ``<script type="application/ld+json">``."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[str] = []
        self._capturing = False
        self._buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "script":
            return
        attributes = {key.lower(): (value or "").lower() for key, value in attrs}
        if attributes.get("type", "").strip() == LD_JSON_TYPE:
            self._capturing = True
            self._buffer = []

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._capturing:
            self._capturing = False
            block = "".join(self._buffer).strip()
            if block:
                self.blocks.append(block)
            self._buffer = []

    def handle_data(self, data: str) -> None:
        if self._capturing:
            self._buffer.append(data)


def extract_job_postings(html_text: str) -> list[dict[str, Any]]:
    """Every ``JobPosting`` object embedded in a page, in document order.

    Tolerates the three shapes real pages use - a single object, a top-level
    array, and an ``@graph`` wrapper - and skips a malformed block rather than
    abandoning the page (a marketing script with trailing commas must not cost us
    the job listings).
    """
    collector = _LdJsonCollector()
    collector.feed(html_text)
    collector.close()

    postings: list[dict[str, Any]] = []

    def visit(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                visit(item)
            return
        if not isinstance(node, dict):
            return
        node_type = node.get("@type")
        types = {str(t).casefold() for t in as_sequence(node_type) if t is not None}
        if "jobposting" in types:
            postings.append(node)
        for key in ("@graph", "itemListElement", "mainEntity"):
            if key in node:
                visit(node[key])

    for block in collector.blocks:
        try:
            visit(json.loads(block))
        except (ValueError, UnicodeDecodeError):
            continue
    return postings


@register
class JsonLdSource(JobSource):
    """Scrape JobPosting JSON-LD from one configured careers page."""

    provider: ClassVar[str] = "json_ld"
    required_config: ClassVar[tuple[str, ...]] = ("url",)

    @property
    def page_url(self) -> str:
        return self.config_str("url")

    @property
    def base_url(self) -> str:
        """Base for resolving relative posting URLs; defaults to the page itself."""
        return str(self.config.get("base_url") or self.page_url)

    def fetch_pages(self) -> Iterator[Sequence[Any]]:
        response = self.client.request(
            self.page_url, headers={"Accept": "text/html,application/xhtml+xml"}
        )
        body = response.text()
        if not body.strip():
            raise ParseError(f"{self.describe()}: {self.page_url} returned an empty body")
        postings = extract_job_postings(body)
        if not postings and "jobposting" not in body.casefold():
            raise ParseError(
                f"{self.describe()}: no JSON-LD JobPosting found at {self.page_url} "
                "(page may be JavaScript-rendered - needs-browser)"
            )
        yield postings

    def normalize(self, raw_job: Any) -> Job:
        record = as_mapping(raw_job)
        if not record:
            raise InvalidJobError(f"{self.describe()}: posting was not an object")

        raw_url = text_of(record, "url", "sameAs", "hiringOrganization.sameAs")
        if not raw_url:
            raise InvalidJobError(f"{self.describe()}: posting {record.get('title')!r} has no url")
        url = urljoin(self.base_url, raw_url)

        return Job(
            company=self.company.company,
            title=text_of(record, "title", "name") or "",
            url=url,
            source=self.provider,
            location=self._location(record),
            external_id=self._identifier(record),
            date_posted=first_of(record, "datePosted", "dateCreated"),
            description=first_of(record, "description"),
            employment_type=self._employment_type(record),
        )

    @staticmethod
    def _identifier(record: Any) -> str | None:
        return text_of(as_mapping(record), "identifier.value", "identifier", "jobPostingId", "@id")

    @staticmethod
    def _employment_type(record: Any) -> str | None:
        value = as_mapping(record).get("employmentType")
        if isinstance(value, list):
            return join_locations(str(v) for v in value)
        return text_of(as_mapping(record), "employmentType")

    @staticmethod
    def _location(record: Any) -> str | None:
        mapping = as_mapping(record)
        labels: list[str | None] = []
        for place in as_sequence(mapping.get("jobLocation")):
            address = as_mapping(as_mapping(place).get("address"))
            labels.append(
                compose_location(
                    address.get("addressLocality"),
                    address.get("addressRegion"),
                    address.get("addressCountry")
                    if isinstance(address.get("addressCountry"), str)
                    else text_of(as_mapping(address.get("addressCountry")), "name"),
                )
            )
        remote = mapping.get("jobLocationType")
        if isinstance(remote, str) and "telecommute" in remote.casefold():
            labels.append("Remote")
        return join_locations(labels)


__all__: Sequence[str] = ("LD_JSON_TYPE", "JsonLdSource", "extract_job_postings")
