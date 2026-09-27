"""A deterministic source whose postings are configured inline.

This exists for the Level-7 local architecture test (PRD §14: *"The test may use
deterministic scraper fixtures while exercising real local AWS-emulated
infrastructure"*). It lets a real Lambda, triggered by a real SQS event source,
run the real pipeline into real (emulated) DynamoDB and SNS **without** depending
on a third-party careers site being reachable or unchanged.

It is not a shortcut around provider testing: each adapter's HTTP behaviour is
covered exhaustively at Level 2 against saved fixtures, and the deployed worker's
outbound HTTP path is exercised separately by polling a real company. What this
isolates is the *architecture* - queues, event sources, retries, dead-lettering,
storage, fan-out - from the weather.

No company in ``companies.json`` uses it, and a test enforces that. Postings are
carried in ``provider_config`` rather than a bundled file, so a fixture company
travels to the worker inside the ordinary :class:`ScrapeTask` message with no
registry entry and no new event field:

    {"provider": "fixture",
     "provider_config": {
        "jobs": [{"title": "Software Engineer Intern",
                  "url": "https://example.test/jobs/1",
                  "external_id": "1",
                  "location": "San Francisco, CA"}]}}

``{"fail": "..."}`` instead of ``jobs`` makes it raise, which is how the
failure-isolation scenario gets a reliably broken scraper.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any, ClassVar

from jobmonitor.errors import InvalidJobError, ParseError, ProviderConfigError
from jobmonitor.models.job import Job
from jobmonitor.scrapers._parse import as_mapping, as_sequence, first_of, text_of
from jobmonitor.scrapers.base import JobSource, register

PROVIDER = "fixture"


@register
class FixtureSource(JobSource):
    provider: ClassVar[str] = PROVIDER
    #: Validated in _validate_config: either `jobs` or `fail`.
    required_config: ClassVar[tuple[str, ...]] = ()

    def _validate_config(self) -> None:
        if not self.config.get("jobs") and not self.config.get("fail"):
            raise ProviderConfigError(
                f"{self.company.company}: fixture adapter requires 'jobs' "
                "(a list of postings) or 'fail' (an error message) in provider_config"
            )

    @property
    def simulated_failure(self) -> str | None:
        failure = self.config.get("fail")
        return str(failure) if failure else None

    def fetch_pages(self) -> Iterator[Sequence[Any]]:
        failure = self.simulated_failure
        if failure:
            raise ParseError(f"{self.describe()}: {failure}")
        yield list(as_sequence(self.config.get("jobs")))

    def normalize(self, raw_job: Any) -> Job:
        record = as_mapping(raw_job)
        if not record:
            raise InvalidJobError(f"{self.describe()}: posting was not an object")
        url = text_of(record, "url")
        if not url:
            raise InvalidJobError(f"{self.describe()}: posting has no url")
        return Job(
            # The company always comes from the registry entry, never the payload,
            # so a fixture cannot attribute a job to somebody else.
            company=self.company.company,
            title=text_of(record, "title") or "",
            url=url,
            source=self.provider,
            location=text_of(record, "location"),
            external_id=text_of(record, "external_id", "id"),
            date_posted=first_of(record, "date_posted", "datePosted"),
            description=first_of(record, "description"),
            employment_type=text_of(record, "employment_type"),
        )


__all__: Sequence[str] = ("PROVIDER", "FixtureSource")
