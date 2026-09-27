"""IBM careers search.

Endpoint (public, unauthenticated - the search API careers.ibm.com calls)::

    POST https://www-api.ibm.com/search/api/v2
    {"appId": "careers", "scopes": ["careers2"],
     "query": {"bool": {"must": [{"simple_query_string":
                {"query": "intern", "fields": ["title^3", "description^2"]}}]}},
     "size": 100, "from": 0, "lang": "zz",
     "_source": ["_id", "title", "url", "description", "field_keyword_08",
                 "field_keyword_17", "field_keyword_18", "field_keyword_19"]}

Response shape encoded by ``tests/fixtures/ibm/*.json``, captured live on
2026-09-27 and trimmed - an Elasticsearch result::

    {"hits": {"total": {"value": 271, "relation": "eq"},
              "hits": [{"_id": "0638a9...",
                        "_source": {"title": "Procurement Intern: 2027",
                                    "url": "https://careers.ibm.com/careers/JobDetail?jobId=129790",
                                    "description": "...",
                                    "field_keyword_08": "Enterprise Operations",
                                    "field_keyword_17": "Hybrid",
                                    "field_keyword_18": "Internship",
                                    "field_keyword_19": "RESEARCH TRIANGLE PARK, US"}}]}}

Notes:

* **No posting date is exposed.** ``_source`` is checked against an allowlist
  (unknown field names return 400), and none of the permitted fields is a date.
  IBM postings are therefore undated, and the one-day window's undated rule
  applies: kept, with the seen-before check stopping repeat alerts.
* The ``field_keyword_NN`` names are IBM's; their meaning (team, work model,
  employment type, location) was read off live results.
* The stable id is the ``jobId`` in the posting URL; ``_id`` is a content hash
  that changes when the posting is edited.
* Every term in ``_search.DEFAULT_QUERIES`` is searched and merged. A multi-word
  term is sent as a quoted phrase: Elasticsearch's ``simple_query_string``
  otherwise ORs the words, and "summer analyst" would match every analyst role.
* Matched on title and description only. Matching the whole body as well (which
  the site's own full-text search does) returns ~1,900 hits, nearly all of them
  because "intern" appears in boilerplate.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any, ClassVar
from urllib.parse import parse_qs, urlsplit

from jobmonitor.errors import InvalidJobError, ParseError
from jobmonitor.models.job import Job
from jobmonitor.scrapers._parse import as_mapping, join_locations, text_of
from jobmonitor.scrapers._search import configured_queries
from jobmonitor.scrapers.base import JobSource, register

API_URL = "https://www-api.ibm.com/search/api/v2"
PAGE_SIZE = 100
_SOURCE_FIELDS = [
    "_id",
    "title",
    "url",
    "description",
    "language",
    "field_keyword_08",
    "field_keyword_17",
    "field_keyword_18",
    "field_keyword_19",
]


@register
class IbmCareersSource(JobSource):
    provider: ClassVar[str] = "ibm"
    required_config: ClassVar[tuple[str, ...]] = ()

    @property
    def queries(self) -> tuple[str, ...]:
        return configured_queries(self.config, "query")

    @property
    def jobs_url(self) -> str:
        return API_URL

    def request_body(self, offset: int, query: str | None = None) -> dict[str, Any]:
        term = query if query is not None else self.queries[0]
        if " " in term and not term.startswith('"'):
            term = f'"{term}"'
        return {
            "appId": "careers",
            "scopes": ["careers2"],
            "query": {
                "bool": {
                    "must": [
                        {
                            "simple_query_string": {
                                "query": term,
                                "fields": ["title^3", "description^2"],
                            }
                        }
                    ]
                }
            },
            "size": PAGE_SIZE,
            "from": offset,
            "lang": "zz",
            "_source": _SOURCE_FIELDS,
        }

    def fetch_pages(self) -> Iterator[Sequence[Any]]:
        yield from self.search_all(self.queries, self._search)

    def _search(self, query: str) -> Iterator[Sequence[Any]]:
        offset = 0
        total: int | None = None
        while True:
            payload = self.client.post_json(API_URL, self.request_body(offset, query))
            hits = as_mapping(as_mapping(payload).get("hits"))
            rows = hits.get("hits")
            if not isinstance(rows, list):
                raise ParseError(
                    f"{self.describe()}: expected Elasticsearch hits at offset {offset}"
                )
            declared = as_mapping(hits.get("total")).get("value", hits.get("total"))
            if total is None and isinstance(declared, int) and declared > 0:
                total = declared
            if not rows:
                return
            yield rows
            offset += len(rows)
            if total is not None:
                if offset >= total:
                    return
            elif len(rows) < PAGE_SIZE:
                # Only without a declared total is a short page the end signal.
                # With one, a short page must not stop us: if the site ever lowers
                # its page size, stopping early would silently drop the rest - the
                # same shape of bug that cut every Workday board at 40 postings.
                return

    def normalize(self, raw_job: Any) -> Job:
        hit = as_mapping(raw_job)
        record = as_mapping(hit.get("_source"))
        if not record:
            raise InvalidJobError(f"{self.describe()}: hit has no _source")
        url = text_of(record, "url")
        if not url:
            raise InvalidJobError(f"{self.describe()}: hit {hit.get('_id')} has no url")

        job_ids = parse_qs(urlsplit(url).query).get("jobId")
        return Job(
            company=self.company.company,
            title=text_of(record, "title") or "",
            url=url,
            source=self.provider,
            location=join_locations([text_of(record, "field_keyword_19")]),
            external_id=job_ids[0] if job_ids else text_of(hit, "_id"),
            date_posted=None,
            description=text_of(record, "description"),
            employment_type=text_of(record, "field_keyword_18"),
        )


__all__: Sequence[str] = ("API_URL", "PAGE_SIZE", "IbmCareersSource")
