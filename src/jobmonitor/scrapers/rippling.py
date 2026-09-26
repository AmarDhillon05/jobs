"""Rippling ATS adapter - 1 company in the shipped registry.

Endpoint (public board API, no authentication):

    GET https://api.rippling.com/platform/api/ats/v1/board/{board_slug}/jobs

Response shape encoded by ``tests/fixtures/rippling/*.json`` - a bare array::

    [{"uuid": "3fd9615a-...",
      "name": "Frontend Software Engineer Intern",
      "url": "https://ats.rippling.com/acme/jobs/3fd9615a-...",
      "workLocation": {"label": "San Francisco, CA", "city": "San Francisco",
                       "state": "CA", "country": "US"},
      "employmentType": "INTERN",
      "department": {"label": "Engineering"},
      "isRemote": false,
      "createdAt": "2026-09-01T00:00:00Z"}]

Rippling is the least-documented provider in this project and could not be
verified live from this sandbox (BLOCKERS.md BLK-001/BLK-002). The adapter is
therefore written defensively: it accepts either an array or a
``{"jobs"/"items"/"results": [...]}`` wrapper, and reads title/url/location from
any of several plausible spellings. That tolerance is cheap and means a key
rename degrades one column instead of failing the whole company.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any, ClassVar

from jobmonitor.errors import InvalidJobError, ParseError
from jobmonitor.models.job import Job
from jobmonitor.scrapers._parse import (
    as_mapping,
    compose_location,
    first_of,
    label_of,
    text_of,
)
from jobmonitor.scrapers.base import JobSource, register

API_ROOT = "https://api.rippling.com/platform/api/ats/v1/board"


@register
class RipplingSource(JobSource):
    provider: ClassVar[str] = "rippling"
    required_config: ClassVar[tuple[str, ...]] = ("board_slug",)

    @property
    def board_slug(self) -> str:
        return self.config_str("board_slug")

    @property
    def jobs_url(self) -> str:
        return f"{API_ROOT}/{self.board_slug}/jobs"

    def fetch_pages(self) -> Iterator[Sequence[Any]]:
        payload = self.client.get_json(self.jobs_url)
        if isinstance(payload, dict):
            wrapped = first_of(payload, "jobs", "items", "results", "data")
            if wrapped is None:
                raise ParseError(
                    f"{self.describe()}: object response had no jobs array "
                    f"(keys: {sorted(payload)[:8]})"
                )
            payload = wrapped
        if not isinstance(payload, list):
            raise ParseError(
                f"{self.describe()}: expected a JSON array from {self.jobs_url}, "
                f"got {type(payload).__name__}"
            )
        yield payload

    def normalize(self, raw_job: Any) -> Job:
        record = as_mapping(raw_job)
        if not record:
            raise InvalidJobError(f"{self.describe()}: job record was not an object")

        identifier = text_of(record, "uuid", "id")
        url = text_of(record, "url", "jobUrl", "applyUrl")
        if not url and identifier:
            url = f"https://ats.rippling.com/{self.board_slug}/jobs/{identifier}"
        if not url:
            raise InvalidJobError(f"{self.describe()}: job {record.get('name')!r} has no URL")

        work_location = as_mapping(record.get("workLocation"))
        location = label_of(record.get("workLocation")) or compose_location(
            work_location.get("city"),
            work_location.get("state"),
            work_location.get("country"),
        )
        if record.get("isRemote") is True:
            location = f"{location}; Remote" if location else "Remote"

        return Job(
            company=self.company.company,
            title=text_of(record, "name", "title") or "",
            url=url,
            source=self.provider,
            location=location,
            external_id=identifier,
            date_posted=first_of(record, "createdAt", "publishedAt", "postedAt"),
            description=first_of(record, "descriptionPlain", "description"),
            employment_type=label_of(record.get("employmentType")),
        )


__all__: Sequence[str] = ("API_ROOT", "RipplingSource")
