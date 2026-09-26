"""Custom scraper: the Simplify community feed as a **secondary fallback**.

PRD §4.3 permits this and only this use: *"Avoid depending on Simplify/Ouckah as
the live detection mechanism unless used only as a secondary fallback or
discovery feed."*

It is configured for exactly the 11 registry entries whose own careers endpoint
could not be configured from public evidence (Stripe, Databricks, Coinbase,
Waymo, Citadel, Citadel Securities, Hudson River Trading, Two Sigma, DE Shaw,
Datadog, Apple - all company-owned sites or bot-protected tenants). Those
companies carry ``support_status: partial`` precisely because this is a
second-hand source:

* it lags the company's own board by however long a human takes to file a PR;
* it only sees roles a contributor noticed;
* its ``date_posted`` is when the *repository* saw the job, not the employer.

Every other company in the registry reads its employer's own endpoint directly,
so a change to this feed can never degrade the other 139.

    GET https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships
        /dev/.github/scripts/listings.json

Response shape encoded by ``tests/fixtures/simplify/listings.json``::

    [{"company_name": "Stripe", "title": "Software Engineer Intern",
      "url": "https://stripe.com/jobs/listing/x/1234",
      "id": "d35d37b7-...", "active": true, "is_visible": true,
      "locations": ["San Francisco", "NYC"], "date_posted": 1769728646,
      "terms": ["Summer 2027"], "source": "Simplify"}]
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any, ClassVar

from jobmonitor.discovery import normalize_company_name
from jobmonitor.errors import InvalidJobError, ParseError
from jobmonitor.models.job import Job
from jobmonitor.scrapers._parse import as_mapping, as_sequence, first_of, join_locations, text_of
from jobmonitor.scrapers.base import JobSource, register

FEED_URL = (
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships"
    "/dev/.github/scripts/listings.json"
)


@register
class SimplifyFallbackSource(JobSource):
    provider: ClassVar[str] = "simplify_fallback"
    #: Which employer names in the feed belong to this registry entry.
    required_config: ClassVar[tuple[str, ...]] = ("company_names",)

    @property
    def feed_url(self) -> str:
        return str(self.config.get("feed_url") or FEED_URL)

    @property
    def wanted(self) -> frozenset[str]:
        names = as_sequence(self.config.get("company_names"))
        return frozenset(normalize_company_name(str(name)) for name in names if name)

    def fetch_pages(self) -> Iterator[Sequence[Any]]:
        payload = self.client.get_json(self.feed_url)
        if not isinstance(payload, list):
            raise ParseError(
                f"{self.describe()}: expected a JSON array from {self.feed_url}, "
                f"got {type(payload).__name__}"
            )
        wanted = self.wanted
        mine = [
            row
            for row in payload
            if normalize_company_name(str(as_mapping(row).get("company_name") or "")) in wanted
        ]
        yield mine

    def normalize(self, raw_job: Any) -> Job:
        record = as_mapping(raw_job)
        if not record:
            raise InvalidJobError(f"{self.describe()}: listing was not an object")

        # A closed or hidden listing must not be notified about: the link is dead.
        if record.get("active") is False or record.get("is_visible") is False:
            raise InvalidJobError(
                f"{self.describe()}: listing {record.get('id')} is inactive/hidden, skipping"
            )

        url = text_of(record, "url")
        if not url:
            raise InvalidJobError(f"{self.describe()}: listing {record.get('id')} has no url")

        return Job(
            company=self.company.company,
            title=text_of(record, "title") or "",
            url=url,
            source=self.provider,
            location=join_locations(str(loc) for loc in as_sequence(record.get("locations"))),
            external_id=text_of(record, "id"),
            date_posted=first_of(record, "date_posted", "date_updated"),
            description=join_locations(
                (
                    text_of(record, "category"),
                    join_locations(str(term) for term in as_sequence(record.get("terms"))),
                ),
                separator=" - ",
            ),
            employment_type="Internship",
        )


__all__: Sequence[str] = ("FEED_URL", "SimplifyFallbackSource")
