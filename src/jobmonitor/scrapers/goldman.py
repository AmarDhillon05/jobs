"""Goldman Sachs careers (higher.gs.com).

Endpoint (public, unauthenticated GraphQL - the API higher.gs.com calls; its
schema is open to introspection, which is where every name below comes from)::

    POST https://api-higher.gs.com/gateway/api/v1/graphql
    query RoleSearch($input: RoleSearchQueryInput!) {
      roleSearch(searchQueryInput: $input) {
        totalCount
        page { pageNumber pageSize hasNext }
        items { roleId jobTitle corporateTitle division lastPostedDate
                shortDescription locations { city state country primary } }
      }
    }
    variables: {"input": {"page": {"pageSize": 100, "pageNumber": 0},
                          "experiences": ["CAMPUS"],
                          "sort": {"sortStrategy": "POSTED_DATE", "sortOrder": "DESC"}}}

Response shape encoded by ``tests/fixtures/goldman/*.json``, captured live on
2026-09-27 and trimmed::

    {"data": {"roleSearch": {
        "totalCount": 282,
        "page": {"pageNumber": 0, "pageSize": 100, "hasNext": true},
        "items": [{"roleId": "170863_GS_CAMPUS",
                   "jobTitle": "2027 | EMEA | London | ... | Summer Analyst",
                   "corporateTitle": "Summer Analyst",
                   "lastPostedDate": "2026-09-25T11:57:00.243Z",
                   "locations": [{"city": "London", "country": "United Kingdom", ...}]}]}}}

Notes:

* **The campus section is read whole; it is not keyword-searched.** Goldman
  titles internships "Summer Analyst", and a search for "intern" finds 1 of the
  282 campus roles. ``experiences`` (default ``["CAMPUS"]``) selects the section.
* GraphQL reports failures as ``{"errors": [...]}`` with HTTP 200, so the adapter
  checks for them explicitly rather than trusting the status code.
* The public role page is ``https://higher.gs.com/roles/{roleId}`` (verified live).
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any, ClassVar

from jobmonitor.errors import InvalidJobError, ParseError
from jobmonitor.models.job import Job
from jobmonitor.scrapers._parse import as_mapping, as_sequence, join_locations, text_of
from jobmonitor.scrapers.base import JobSource, register

API_URL = "https://api-higher.gs.com/gateway/api/v1/graphql"
ROLE_URL = "https://higher.gs.com/roles/{role_id}"
PAGE_SIZE = 100
DEFAULT_EXPERIENCES = ("CAMPUS",)

QUERY = """query RoleSearch($input: RoleSearchQueryInput!) {
  roleSearch(searchQueryInput: $input) {
    totalCount
    page { pageNumber pageSize hasNext }
    items {
      roleId jobTitle corporateTitle division lastPostedDate shortDescription
      locations { city state country primary }
    }
  }
}"""


@register
class GoldmanSachsSource(JobSource):
    provider: ClassVar[str] = "goldman"
    required_config: ClassVar[tuple[str, ...]] = ()

    @property
    def experiences(self) -> list[str]:
        configured = self.config.get("experiences")
        if isinstance(configured, (list, tuple)) and configured:
            return [str(item) for item in configured]
        return list(DEFAULT_EXPERIENCES)

    @property
    def jobs_url(self) -> str:
        return API_URL

    def request_body(self, page: int) -> dict[str, Any]:
        return {
            "operationName": "RoleSearch",
            "query": QUERY,
            "variables": {
                "input": {
                    "page": {"pageSize": PAGE_SIZE, "pageNumber": page},
                    "experiences": self.experiences,
                    "sort": {"sortStrategy": "POSTED_DATE", "sortOrder": "DESC"},
                }
            },
        }

    def fetch_pages(self) -> Iterator[Sequence[Any]]:
        page = 0
        while True:
            payload = as_mapping(self.client.post_json(API_URL, self.request_body(page)))
            errors = payload.get("errors")
            if errors:
                first = as_mapping(as_sequence(errors)[0] if as_sequence(errors) else {})
                raise ParseError(
                    f"{self.describe()}: GraphQL error on page {page}: "
                    f"{text_of(first, 'message') or errors!r}"
                )
            search = as_mapping(as_mapping(payload.get("data")).get("roleSearch"))
            items = search.get("items")
            if not isinstance(items, list):
                raise ParseError(f"{self.describe()}: page {page} has no roleSearch.items list")
            if not items:
                return
            yield items
            if not as_mapping(search.get("page")).get("hasNext"):
                return
            page += 1

    def normalize(self, raw_job: Any) -> Job:
        record = as_mapping(raw_job)
        if not record:
            raise InvalidJobError(f"{self.describe()}: role was not an object")
        role_id = text_of(record, "roleId")
        if not role_id:
            raise InvalidJobError(f"{self.describe()}: role has no roleId")

        locations = []
        for place in as_sequence(record.get("locations")):
            where = as_mapping(place)
            parts = [text_of(where, key) for key in ("city", "state", "country")]
            locations.append(", ".join(part for part in parts if part) or None)

        return Job(
            company=self.company.company,
            title=text_of(record, "jobTitle") or "",
            url=ROLE_URL.format(role_id=role_id),
            source=self.provider,
            location=join_locations(locations),
            external_id=role_id,
            date_posted=record.get("lastPostedDate"),
            description=text_of(record, "shortDescription"),
            employment_type=text_of(record, "corporateTitle"),
        )


__all__: Sequence[str] = ("API_URL", "PAGE_SIZE", "QUERY", "GoldmanSachsSource")
