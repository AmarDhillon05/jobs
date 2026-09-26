"""Lever postings adapter - 13 companies in the shipped registry.

Endpoint (public, documented, no authentication):

    GET https://api.lever.co/v0/postings/{site}?mode=json

Response shape encoded by ``tests/fixtures/lever/*.json`` - a bare JSON *array*::

    [{"id": "0f1e2d3c-...",
      "text": "Software Engineer Intern",
      "hostedUrl": "https://jobs.lever.co/acme/0f1e2d3c-...",
      "applyUrl": "https://jobs.lever.co/acme/0f1e2d3c-.../apply",
      "categories": {"commitment": "Intern", "department": "Engineering",
                     "location": "San Francisco", "team": "Core"},
      "workplaceType": "onsite",
      "createdAt": 1769728646000,
      "descriptionPlain": "plain text",
      "description": "<div>html</div>",
      "lists": [{"text": "Requirements", "content": "<ul>..."}]}]

Notes:

* ``createdAt`` is epoch **milliseconds**; ``Job`` handles the conversion.
* The description is split across ``description`` plus a ``lists`` array; the
  adapter stitches them back together so an email preview is useful.
* Lever returns the whole board in one array, so ``fetch_pages`` yields once.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any, ClassVar

from jobmonitor.errors import InvalidJobError, ParseError
from jobmonitor.models.job import Job
from jobmonitor.scrapers._parse import as_mapping, as_sequence, first_of, join_locations, text_of
from jobmonitor.scrapers.base import JobSource, register

API_ROOT = "https://api.lever.co/v0/postings"


@register
class LeverSource(JobSource):
    provider: ClassVar[str] = "lever"
    required_config: ClassVar[tuple[str, ...]] = ("site",)

    @property
    def site(self) -> str:
        return self.config_str("site")

    @property
    def jobs_url(self) -> str:
        return f"{API_ROOT}/{self.site}?mode=json"

    def fetch_pages(self) -> Iterator[Sequence[Any]]:
        payload = self.client.get_json(self.jobs_url)
        if isinstance(payload, dict):
            # Some Lever tenants wrap the array; accept both rather than fail.
            wrapped = first_of(payload, "data", "postings", "results")
            if wrapped is None:
                raise ParseError(
                    f"{self.describe()}: object response had no postings array "
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
            raise InvalidJobError(f"{self.describe()}: posting was not an object")

        url = text_of(record, "hostedUrl", "applyUrl")
        if not url:
            raise InvalidJobError(f"{self.describe()}: posting {record.get('id')} has no URL")

        categories = as_mapping(record.get("categories"))
        locations = [text_of(categories, "location", "allLocations")]
        locations.extend(
            text_of({"v": v}, "v") for v in as_sequence(categories.get("allLocations"))
        )
        workplace = text_of(record, "workplaceType")
        if workplace and workplace.casefold() == "remote":
            locations.append("Remote")

        return Job(
            company=self.company.company,
            title=text_of(record, "text", "title") or "",
            url=url,
            source=self.provider,
            location=join_locations(locations),
            external_id=text_of(record, "id"),
            date_posted=first_of(record, "createdAt", "updatedAt", "createdAtIso"),
            description=self._description(record),
            employment_type=text_of(categories, "commitment"),
        )

    @staticmethod
    def _description(record: Any) -> str | None:
        """Stitch the body and the `lists` blocks back into one document."""
        mapping = as_mapping(record)
        parts: list[str] = []
        body = text_of(mapping, "descriptionPlain") or mapping.get("description")
        if isinstance(body, str) and body.strip():
            parts.append(body)
        for block in as_sequence(mapping.get("lists")):
            item = as_mapping(block)
            heading = text_of(item, "text")
            content = item.get("content")
            if heading:
                parts.append(f"{heading}:")
            if isinstance(content, str) and content.strip():
                parts.append(content)
        return " ".join(parts) if parts else None


__all__: Sequence[str] = ("API_ROOT", "LeverSource")
