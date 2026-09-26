"""Workable adapter.

Endpoint (public account widget, no authentication):

    GET https://apply.workable.com/api/v1/widget/accounts/{subdomain}?details=true

Response shape encoded by ``tests/fixtures/workable/*.json``::

    {"name": "Acme", "description": "...",
     "jobs": [{"title": "Software Engineer Intern",
               "shortcode": "ABC123DEF",
               "code": "ENG-1",
               "employment_type": "Intern",
               "telecommuting": false,
               "department": "Engineering",
               "url": "https://apply.workable.com/acme/j/ABC123DEF/",
               "application_url": "https://apply.workable.com/acme/j/ABC123DEF/apply/",
               "published_on": "2026-09-01",
               "created_at": "2026-09-01",
               "country": "United States", "city": "Austin", "state": "Texas",
               "description": "<p>...</p>", "requirements": "<ul>...", "benefits": ""}]}

No company in the shipped registry currently uses Workable, but 62 employers in
the discovery snapshot do, so the adapter exists to make those a configuration
change rather than a code change (PRD §6: reusable adapters over bespoke ones).
It is covered by the same fixture test suite as every other provider.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any, ClassVar

from jobmonitor.errors import InvalidJobError, ParseError
from jobmonitor.models.job import Job
from jobmonitor.scrapers._parse import as_mapping, compose_location, first_of, text_of
from jobmonitor.scrapers.base import JobSource, register

API_ROOT = "https://apply.workable.com/api/v1/widget/accounts"


@register
class WorkableSource(JobSource):
    provider: ClassVar[str] = "workable"
    required_config: ClassVar[tuple[str, ...]] = ("subdomain",)

    @property
    def subdomain(self) -> str:
        return self.config_str("subdomain")

    @property
    def jobs_url(self) -> str:
        return f"{API_ROOT}/{self.subdomain}?details=true"

    def fetch_pages(self) -> Iterator[Sequence[Any]]:
        payload = self.client.get_json(self.jobs_url)
        if not isinstance(payload, dict):
            raise ParseError(
                f"{self.describe()}: expected a JSON object from {self.jobs_url}, "
                f"got {type(payload).__name__}"
            )
        jobs = payload.get("jobs")
        if jobs is None:
            raise ParseError(f"{self.describe()}: response has no 'jobs' key")
        if not isinstance(jobs, list):
            raise ParseError(f"{self.describe()}: 'jobs' was {type(jobs).__name__}, expected list")
        yield jobs

    def normalize(self, raw_job: Any) -> Job:
        record = as_mapping(raw_job)
        if not record:
            raise InvalidJobError(f"{self.describe()}: job record was not an object")

        shortcode = text_of(record, "shortcode")
        url = text_of(record, "url", "application_url")
        if not url and shortcode:
            url = f"https://apply.workable.com/{self.subdomain}/j/{shortcode}/"
        if not url:
            raise InvalidJobError(f"{self.describe()}: job {record.get('title')!r} has no URL")

        location = compose_location(record.get("city"), record.get("state"), record.get("country"))
        if record.get("telecommuting") is True:
            location = f"{location}; Remote" if location else "Remote"

        description_parts = [record.get(key) for key in ("description", "requirements", "benefits")]
        description = " ".join(part for part in description_parts if isinstance(part, str) and part)

        return Job(
            company=self.company.company,
            title=text_of(record, "title") or "",
            url=url,
            source=self.provider,
            location=location,
            external_id=shortcode or text_of(record, "id", "code"),
            date_posted=first_of(record, "published_on", "created_at"),
            description=description or None,
            employment_type=text_of(record, "employment_type"),
        )


__all__: Sequence[str] = ("API_ROOT", "WorkableSource")
