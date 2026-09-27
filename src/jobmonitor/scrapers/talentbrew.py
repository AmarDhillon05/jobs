"""Radancy TalentBrew career sites - Arm.

Endpoint (public, unauthenticated - the XHR the site's own search makes). It
returns JSON whose ``results`` field is a server-rendered HTML fragment::

    GET https://{host}/search-jobs/results?Keywords={query}&CurrentPage={n}
        &RecordsPerPage=100&SearchType=5&...

The fragment, captured live from ``careers.arm.com`` on 2026-09-27 and trimmed
into ``tests/fixtures/talentbrew/*.json``::

    <section id="search-results" data-total-job-results="66" data-total-pages="1"
             data-current-page="1" ...>
      <li class="job-card ...">
        <a class="job-card__title" href="/job/trondheim/software-developer-intern/33099/1006..."
           data-job-id="100604944176">Software Developer Intern</a>
        <span class="job-card__intro">Automate processes, ...</span>
        <span class="location">Trondheim, Norway</span>
        <span class="category">Software Engineering, Intern</span>
      </li>

Notes:

* **No posting date is exposed.** The dates visible elsewhere in the fragment
  belong to editorial content cards, not jobs. Postings are undated, so the
  one-day window's undated rule applies.
* The paging attributes live on the ``<section>``; pages are requested until
  ``data-total-pages`` is reached.
* Parsing is by class name, not by position, so extra markup does not shift it.
  A card without a job link is counted as malformed rather than guessed at.
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterator, Sequence
from typing import Any, ClassVar
from urllib.parse import quote

from jobmonitor.errors import InvalidJobError, ParseError
from jobmonitor.models.job import Job
from jobmonitor.scrapers._parse import as_mapping, join_locations
from jobmonitor.scrapers._search import configured_queries
from jobmonitor.scrapers.base import JobSource, register

PAGE_SIZE = 100

_CARD = re.compile(r"<li[^>]*class=\"[^\"]*job-card[^\"]*\"[^>]*>(?P<body>.*?)</li>", re.S)
_LINK = re.compile(
    r"<a[^>]*class=\"[^\"]*job-card__title[^\"]*\"[^>]*href=\"(?P<href>[^\"]+)\"[^>]*>(?P<title>.*?)</a>",
    re.S,
)
_JOB_ID = re.compile(r"data-job-id=\"(?P<id>[^\"]+)\"")
_TOTAL_PAGES = re.compile(r"data-total-pages=\"(?P<n>\d+)\"")


def _span(body: str, css_class: str) -> str | None:
    match = re.search(
        rf"<span[^>]*class=\"[^\"]*\b{re.escape(css_class)}\b[^\"]*\"[^>]*>(.*?)</span>",
        body,
        re.S,
    )
    return _text(match.group(1)) if match else None


def _text(fragment: str) -> str | None:
    value = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", fragment))).strip()
    return value or None


def parse_cards(fragment: str) -> list[dict[str, Any]]:
    """Every job card in a results fragment, as a plain record."""
    records: list[dict[str, Any]] = []
    for card in _CARD.finditer(fragment):
        body = card.group("body")
        link = _LINK.search(body)
        job_id = _JOB_ID.search(body)
        records.append(
            {
                "href": link.group("href") if link else None,
                "title": _text(link.group("title")) if link else None,
                "id": job_id.group("id") if job_id else None,
                "location": _span(body, "location"),
                "category": _span(body, "category"),
                "intro": _span(body, "job-card__intro"),
            }
        )
    return records


@register
class TalentBrewSource(JobSource):
    provider: ClassVar[str] = "talentbrew"
    required_config: ClassVar[tuple[str, ...]] = ("host",)

    @property
    def host(self) -> str:
        return self.config_str("host")

    @property
    def queries(self) -> tuple[str, ...]:
        return configured_queries(self.config, "keywords")

    def page_url(self, page: int, query: str | None = None) -> str:
        term = quote(query if query is not None else self.queries[0])
        return (
            f"https://{self.host}/search-jobs/results?ActiveFacetID=0&CurrentPage={page + 1}"
            f"&RecordsPerPage={PAGE_SIZE}&Distance=50&RadiusUnitType=0&Keywords={term}"
            "&Location=&ShowRadius=False&IsPagination=False&CustomFacetName=&FacetTerm="
            "&FacetType=0&SearchResultsModuleName=Search+Results"
            "&SearchFiltersModuleName=Search+Filters&SortCriteria=0&SortDirection=0&SearchType=5"
        )

    @property
    def jobs_url(self) -> str:
        return self.page_url(0)

    def fetch_pages(self) -> Iterator[Sequence[Any]]:
        yield from self.search_all(self.queries, self._search)

    def _search(self, query: str) -> Iterator[Sequence[Any]]:
        page = 0
        while True:
            payload = as_mapping(
                self.client.get_json(
                    self.page_url(page, query), headers={"X-Requested-With": "XMLHttpRequest"}
                )
            )
            fragment = payload.get("results")
            if not isinstance(fragment, str):
                raise ParseError(
                    f"{self.describe()}: page {page + 1} has no 'results' HTML "
                    f"(keys: {sorted(payload)[:6]})"
                )
            cards = parse_cards(fragment)
            if not cards:
                return
            yield cards
            pages = _TOTAL_PAGES.search(fragment)
            page += 1
            if pages is None or page >= int(pages.group("n")):
                return

    def normalize(self, raw_job: Any) -> Job:
        record = as_mapping(raw_job)
        href = record.get("href")
        if not isinstance(href, str) or not href:
            raise InvalidJobError(f"{self.describe()}: job card has no link")
        url = href if href.startswith("http") else f"https://{self.host}{href}"
        return Job(
            company=self.company.company,
            title=str(record.get("title") or ""),
            url=url,
            source=self.provider,
            location=join_locations([record.get("location")]),
            external_id=record.get("id"),
            date_posted=None,
            description=record.get("intro"),
            employment_type=record.get("category"),
        )


__all__: Sequence[str] = ("PAGE_SIZE", "TalentBrewSource", "parse_cards")
