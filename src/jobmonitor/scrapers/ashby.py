"""Ashby job-board adapter - 24 companies in the shipped registry.

Endpoint (public posting API, no authentication):

    GET https://api.ashbyhq.com/posting-api/job-board/{job_board_name}

Response shape encoded by ``tests/fixtures/ashby/*.json``::

    {"apiVersion": "1",
     "jobs": [{"id": "1ba0a1ef-...",
               "title": "Software Engineer Intern",
               "location": "San Francisco",
               "secondaryLocations": [{"location": "New York"}],
               "department": "Engineering",
               "team": "Core",
               "employmentType": "Intern",
               "isListed": true,
               "publishedAt": "2026-09-01T00:00:00.000Z",
               "jobUrl": "https://jobs.ashbyhq.com/acme/1ba0a1ef-...",
               "applyUrl": "https://jobs.ashbyhq.com/acme/1ba0a1ef-.../application",
               "descriptionHtml": "<p>...</p>",
               "descriptionPlain": "..."}]}

``isListed: false`` postings are unlisted drafts - they are skipped, because
notifying about a role that is not on the public board would send the user to a
dead link.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any, ClassVar

from jobmonitor.errors import InvalidJobError, ParseError
from jobmonitor.models.job import Job
from jobmonitor.scrapers._parse import (
    as_mapping,
    as_sequence,
    first_of,
    join_locations,
    label_of,
    text_of,
)
from jobmonitor.scrapers.base import JobSource, register

API_ROOT = "https://api.ashbyhq.com/posting-api/job-board"


@register
class AshbySource(JobSource):
    provider: ClassVar[str] = "ashby"
    required_config: ClassVar[tuple[str, ...]] = ("job_board_name",)

    @property
    def job_board_name(self) -> str:
        return self.config_str("job_board_name")

    @property
    def jobs_url(self) -> str:
        return f"{API_ROOT}/{self.job_board_name}?includeCompensation=true"

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

        # Explicitly compare to False: a board that omits the flag is still public.
        if record.get("isListed") is False:
            raise InvalidJobError(
                f"{self.describe()}: posting {record.get('id')} is unlisted, skipping"
            )

        url = text_of(record, "jobUrl", "applyUrl")
        if not url:
            raise InvalidJobError(f"{self.describe()}: posting {record.get('id')} has no jobUrl")

        locations = [label_of(record.get("location"), "location", "name", "label")]
        for secondary in as_sequence(record.get("secondaryLocations")):
            locations.append(label_of(secondary, "location", "name", "label"))
        address = as_mapping(first_of(record, "address.postalAddress"))
        if address:
            locations.append(
                join_locations(
                    (
                        text_of(address, "addressLocality"),
                        text_of(address, "addressRegion"),
                    ),
                    separator=", ",
                )
            )

        return Job(
            company=self.company.company,
            title=text_of(record, "title") or "",
            url=url,
            source=self.provider,
            location=join_locations(locations),
            external_id=text_of(record, "id"),
            date_posted=first_of(record, "publishedAt", "updatedAt"),
            description=first_of(record, "descriptionPlain", "descriptionHtml"),
            employment_type=text_of(record, "employmentType"),
        )


__all__: Sequence[str] = ("API_ROOT", "AshbySource")
