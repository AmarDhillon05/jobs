"""SmartRecruiters adapter - 5 companies in the shipped registry. **Paginated.**

Endpoint (public posting API, no authentication):

    GET https://api.smartrecruiters.com/v1/companies/{company_id}/postings
        ?limit=100&offset=0

Response shape encoded by ``tests/fixtures/smartrecruiters/*.json``::

    {"offset": 0, "limit": 100, "totalFound": 250,
     "content": [{"id": "744000012345678",
                  "name": "Software Engineer Intern",
                  "uuid": "...", "refNumber": "REF-1",
                  "releasedDate": "2026-09-01T00:00:00.000Z",
                  "company": {"identifier": "Acme", "name": "Acme"},
                  "location": {"city": "Sydney", "region": "NSW",
                               "country": "au", "remote": false},
                  "typeOfEmployment": {"label": "Intern"},
                  "department": {"label": "Engineering"},
                  "experienceLevel": {"label": "Internship"},
                  "ref": "https://api.smartrecruiters.com/v1/companies/Acme/postings/744000012345678"}]}

Pagination is offset/limit against ``totalFound``. The loop stops on three
conditions - reaching ``totalFound``, an empty page, or no forward progress -
because a provider that ignores ``offset`` would otherwise loop forever handing
back page one.

The posting API returns no description, so the adapter builds the public apply
URL (``https://jobs.smartrecruiters.com/{company}/{id}``) and leaves
``description`` empty rather than issuing 100 extra detail requests per poll.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any, ClassVar

from jobmonitor.errors import InvalidJobError, ParseError
from jobmonitor.models.job import Job
from jobmonitor.scrapers._parse import as_mapping, compose_location, first_of, label_of, text_of
from jobmonitor.scrapers.base import JobSource, register

API_ROOT = "https://api.smartrecruiters.com/v1/companies"
PUBLIC_ROOT = "https://jobs.smartrecruiters.com"
PAGE_SIZE = 100


@register
class SmartRecruitersSource(JobSource):
    provider: ClassVar[str] = "smartrecruiters"
    required_config: ClassVar[tuple[str, ...]] = ("company_id",)

    @property
    def company_id(self) -> str:
        return self.config_str("company_id")

    def page_url(self, offset: int) -> str:
        return f"{API_ROOT}/{self.company_id}/postings?limit={PAGE_SIZE}&offset={offset}"

    def fetch_pages(self) -> Iterator[Sequence[Any]]:
        offset = 0
        seen = 0
        total: int | None = None

        while True:
            url = self.page_url(offset)
            payload = self.client.get_json(url)
            if not isinstance(payload, dict):
                raise ParseError(
                    f"{self.describe()}: expected a JSON object from {url}, "
                    f"got {type(payload).__name__}"
                )
            content = payload.get("content")
            if content is None:
                raise ParseError(f"{self.describe()}: page at offset {offset} has no 'content'")
            if not isinstance(content, list):
                raise ParseError(
                    f"{self.describe()}: 'content' was {type(content).__name__}, expected list"
                )

            declared = payload.get("totalFound")
            if isinstance(declared, int):
                total = declared

            if not content:
                return
            yield content

            seen += len(content)
            offset += len(content)
            if total is not None and seen >= total:
                return
            # A provider ignoring `offset` would hand back page one forever.
            if len(content) < PAGE_SIZE:
                return

    def normalize(self, raw_job: Any) -> Job:
        record = as_mapping(raw_job)
        if not record:
            raise InvalidJobError(f"{self.describe()}: posting was not an object")

        posting_id = text_of(record, "id", "uuid")
        if not posting_id:
            raise InvalidJobError(f"{self.describe()}: posting has no id")

        company_identifier = text_of(record, "company.identifier") or self.company_id
        url = f"{PUBLIC_ROOT}/{company_identifier}/{posting_id}"

        location = as_mapping(record.get("location"))
        parts = compose_location(
            location.get("city"), location.get("region"), location.get("country")
        )
        if location.get("remote") is True:
            parts = f"{parts}; Remote" if parts else "Remote"

        return Job(
            company=self.company.company,
            title=text_of(record, "name", "title") or "",
            url=url,
            source=self.provider,
            location=parts,
            external_id=posting_id,
            date_posted=first_of(record, "releasedDate", "createdOn", "lastActivityOn"),
            description=text_of(record, "jobAd.sections.jobDescription.text"),
            employment_type=label_of(record.get("typeOfEmployment"))
            or label_of(record.get("experienceLevel")),
        )


__all__: Sequence[str] = ("API_ROOT", "PAGE_SIZE", "PUBLIC_ROOT", "SmartRecruitersSource")
