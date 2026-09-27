"""Atlassian careers listings.

Endpoint (public, unauthenticated - what atlassian.com/company/careers loads):

    GET https://www.atlassian.com/endpoint/careers/listings

Response shape encoded by ``tests/fixtures/atlassian/listings.json``, captured
live on 2026-09-27 and trimmed - a bare JSON array of every open role::

    [{"id": 25583,
      "title": "Account Executive - Japanese Speaking",
      "locations": ["Remote - Japan - Remote", "Remote - Remote"],
      "category": "Sales",
      "overview": "<p>...</p>",
      "portalJobPost": {"portalUrl": "https://globalcareers-atlassian.icims.com/...",
                        "updatedDate": "2026-09-22 12:42 AM"}}]

Notes:

* One request returns the whole board (~290 roles), so nothing pages.
* **Only an updated date is exposed**, in Atlassian's own ``YYYY-MM-DD hh:mm AM``
  format, which the generic date parser does not read - hence the explicit
  ``strptime`` here. An update always follows publication, so under the one-day
  window this can admit an old posting that was recently edited but can never
  hide a new one.
* The link sent to the user is Atlassian's own page,
  ``/company/careers/details/{id}`` (verified live); the iCIMS ``portalUrl`` is a
  fallback.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from typing import Any, ClassVar

from jobmonitor.errors import InvalidJobError, ParseError
from jobmonitor.models.job import Job
from jobmonitor.scrapers._parse import as_mapping, as_sequence, join_locations, text_of
from jobmonitor.scrapers.base import JobSource, register

LISTINGS_URL = "https://www.atlassian.com/endpoint/careers/listings"
DETAILS_URL = "https://www.atlassian.com/company/careers/details/{id}"
_UPDATED_FORMAT = "%Y-%m-%d %I:%M %p"


def parse_updated(value: str | None) -> datetime | None:
    """``"2026-09-22 12:42 AM"`` -> an aware UTC datetime, or None."""
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), _UPDATED_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None


@register
class AtlassianSource(JobSource):
    provider: ClassVar[str] = "atlassian"
    required_config: ClassVar[tuple[str, ...]] = ()

    @property
    def jobs_url(self) -> str:
        return LISTINGS_URL

    def fetch_pages(self) -> Iterator[Sequence[Any]]:
        payload = self.client.get_json(LISTINGS_URL)
        if not isinstance(payload, list):
            raise ParseError(
                f"{self.describe()}: expected a JSON array, got {type(payload).__name__}"
            )
        yield payload

    def normalize(self, raw_job: Any) -> Job:
        record = as_mapping(raw_job)
        if not record:
            raise InvalidJobError(f"{self.describe()}: listing was not an object")
        portal = as_mapping(record.get("portalJobPost"))
        identifier = text_of(record, "id") or text_of(portal, "id")
        if identifier:
            url = DETAILS_URL.format(id=identifier)
        else:
            url = text_of(portal, "portalUrl") or ""
        if not url:
            raise InvalidJobError(f"{self.describe()}: listing has neither id nor portal url")

        locations = [text_of({"v": value}, "v") for value in as_sequence(record.get("locations"))]
        return Job(
            company=self.company.company,
            title=text_of(record, "title") or "",
            url=url,
            source=self.provider,
            location=join_locations(locations),
            external_id=identifier,
            date_posted=parse_updated(text_of(portal, "updatedDate")),
            description=text_of(record, "overview"),
            employment_type=text_of(record, "type"),
        )


__all__: Sequence[str] = ("DETAILS_URL", "LISTINGS_URL", "AtlassianSource", "parse_updated")
