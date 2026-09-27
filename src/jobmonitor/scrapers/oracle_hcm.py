"""Oracle Fusion HCM "Candidate Experience" sites - JPMorgan Chase, Oracle, Uber.

Endpoint (public, unauthenticated - the REST call the careers site itself makes):

    GET https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions
        ?onlyData=true&expand=requisitionList.secondaryLocations
        &finder=findReqs;siteNumber={site},keyword={keyword},limit=200,
                offset={offset},sortBy=POSTING_DATES_DESC

Response shape encoded by ``tests/fixtures/oracle_hcm/*.json``, captured live from
``jpmc.fa.oraclecloud.com`` on 2026-09-27 and trimmed::

    {"items": [{"TotalJobsCount": 3870,
                "requisitionList": [{"Id": "210778204",
                                     "Title": "Credit Risk ... Lead",
                                     "PostedDate": "2026-09-27",
                                     "PrimaryLocation": "Mumbai, Maharashtra, India",
                                     "secondaryLocations": [{"Name": "..."}],
                                     "ShortDescriptionStr": "..."}]}],
     "count": 1, "hasMore": false, "limit": 25, "offset": 0}

Notes:

* The requisitions sit one level down, in ``items[0].requisitionList``; the outer
  ``count``/``hasMore`` describe the *search* object, not the jobs. The real
  total is ``items[0].TotalJobsCount``.
* ``limit`` is capped at 200 server-side (asking for 500 returns 200), so a board
  with 3,870 matches takes 20 requests.
* ``PostedDate`` is a bare date. The one-day window reads a bare date as "some
  time that day", so a posting from late yesterday is not lost.
* The public job page is ``/hcmUI/CandidateExperience/{lang}/sites/{site}/job/{Id}``.
* Every term in ``_search.DEFAULT_QUERIES`` is searched and merged. JPMorgan's
  whole board is 7,496 roles; "intern" alone returns a loose 3,870 that is not
  guaranteed to contain its "Summer Analyst" roles.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any, ClassVar
from urllib.parse import quote

from jobmonitor.errors import InvalidJobError, ParseError
from jobmonitor.models.job import Job
from jobmonitor.scrapers._parse import as_mapping, as_sequence, join_locations, text_of
from jobmonitor.scrapers._search import configured_queries
from jobmonitor.scrapers.base import JobSource, register

PAGE_SIZE = 200


@register
class OracleHcmSource(JobSource):
    provider: ClassVar[str] = "oracle_hcm"
    required_config: ClassVar[tuple[str, ...]] = ("host", "site")

    @property
    def host(self) -> str:
        return self.config_str("host")

    @property
    def site(self) -> str:
        return self.config_str("site")

    @property
    def queries(self) -> tuple[str, ...]:
        return configured_queries(self.config, "keyword")

    @property
    def language(self) -> str:
        return str(self.config.get("language", "en"))

    def page_url(self, offset: int, query: str | None = None) -> str:
        keyword = quote(query if query is not None else self.queries[0])
        finder = (
            f"findReqs;siteNumber={self.site},keyword={keyword},"
            f"limit={PAGE_SIZE},offset={offset},sortBy=POSTING_DATES_DESC"
        )
        return (
            f"https://{self.host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
            f"?onlyData=true&expand=requisitionList.secondaryLocations&finder={finder}"
        )

    @property
    def jobs_url(self) -> str:
        return self.page_url(0)

    def job_page(self, requisition_id: str) -> str:
        return (
            f"https://{self.host}/hcmUI/CandidateExperience/{self.language}"
            f"/sites/{self.site}/job/{requisition_id}"
        )

    def fetch_pages(self) -> Iterator[Sequence[Any]]:
        yield from self.search_all(self.queries, self._search)

    def _search(self, query: str) -> Iterator[Sequence[Any]]:
        offset = 0
        total: int | None = None
        while True:
            payload = self.client.get_json(self.page_url(offset, query))
            items = as_sequence(as_mapping(payload).get("items"))
            if not isinstance(payload, dict) or not items:
                raise ParseError(
                    f"{self.describe()}: expected {{'items': [search]}} at offset {offset}"
                )
            search = as_mapping(items[0])
            requisitions = search.get("requisitionList")
            if not isinstance(requisitions, list):
                raise ParseError(
                    f"{self.describe()}: search result has no 'requisitionList' "
                    f"(keys: {sorted(search)[:8]})"
                )
            declared = search.get("TotalJobsCount")
            if total is None and isinstance(declared, int) and declared > 0:
                total = declared
            if not requisitions:
                return
            yield requisitions
            offset += len(requisitions)
            if total is not None:
                if offset >= total:
                    return
            elif len(requisitions) < PAGE_SIZE:
                # Only without a declared total is a short page the end signal.
                # With one, a short page must not stop us: if the site ever lowers
                # its page size, stopping early would silently drop the rest - the
                # same shape of bug that cut every Workday board at 40 postings.
                return

    def normalize(self, raw_job: Any) -> Job:
        record = as_mapping(raw_job)
        if not record:
            raise InvalidJobError(f"{self.describe()}: requisition was not an object")
        identifier = text_of(record, "Id", "RequisitionId")
        if not identifier:
            raise InvalidJobError(f"{self.describe()}: requisition has no Id")

        locations = [text_of(record, "PrimaryLocation")]
        for secondary in as_sequence(record.get("secondaryLocations")):
            locations.append(text_of(as_mapping(secondary), "Name"))

        return Job(
            company=self.company.company,
            title=text_of(record, "Title") or "",
            url=self.job_page(identifier),
            source=self.provider,
            location=join_locations(locations),
            external_id=identifier,
            date_posted=record.get("PostedDate"),
            description=text_of(record, "ShortDescriptionStr", "ExternalDescriptionStr"),
            employment_type=text_of(record, "JobType", "WorkerType"),
        )


__all__: Sequence[str] = ("PAGE_SIZE", "OracleHcmSource")
