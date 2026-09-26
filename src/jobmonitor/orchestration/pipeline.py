"""The pipeline: scrape -> normalize -> filter -> fingerprint -> persist -> notify.

One function does the per-company work (:func:`process_company`) and one class
drives a whole poll (:class:`PollRunner`). Both are plain Python with injected
dependencies, so the identical code path runs three ways:

* in-process, in tests and ``make local`` (in-memory repositories, fake transports);
* inside a worker Lambda, one shard per invocation;
* against LocalStack, with real SQS/SNS/DynamoDB APIs.

That is deliberate: the Level-7 architecture test and the Level-8 acceptance
scenarios exercise the same logic, so a difference between them points at the
infrastructure rather than at a second implementation of the pipeline.

The failure-isolation rule (PRD §16) lives here: every company is processed inside
its own try/except and its own health record. A company that raises produces a
failure record and nothing more - the remaining companies in the shard still run.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from jobmonitor.config import Settings
from jobmonitor.filtering import JobFilter
from jobmonitor.http import HttpClient
from jobmonitor.models.company import Company
from jobmonitor.models.health import PollSummary, ScraperHealth, ScraperStatus, utcnow
from jobmonitor.models.record import JobRecord
from jobmonitor.notifications.notifier import NotificationOutcome, Notifier
from jobmonitor.scrapers.base import JobSource, build_source, safe_fetch
from jobmonitor.storage.base import HealthRepository, JobRepository

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class CompanyOutcome:
    """Everything one company's turn produced."""

    company: str
    health: ScraperHealth
    new_records: list[JobRecord] = field(default_factory=list)
    updated_records: list[JobRecord] = field(default_factory=list)
    seen_records: list[JobRecord] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.health.ok

    @property
    def notifiable(self) -> list[JobRecord]:
        return [record for record in self.new_records if not record.notification_sent]


@dataclass(slots=True)
class PollOutcome:
    """Everything a whole poll produced."""

    poll_id: str
    outcomes: list[CompanyOutcome] = field(default_factory=list)
    notification: NotificationOutcome | None = None
    duration_ms: int = 0

    @property
    def health_records(self) -> list[ScraperHealth]:
        return [outcome.health for outcome in self.outcomes]

    @property
    def new_records(self) -> list[JobRecord]:
        return [record for outcome in self.outcomes for record in outcome.new_records]

    @property
    def updated_records(self) -> list[JobRecord]:
        return [record for outcome in self.outcomes for record in outcome.updated_records]

    @property
    def failures(self) -> list[CompanyOutcome]:
        return [outcome for outcome in self.outcomes if not outcome.ok]

    @property
    def successes(self) -> list[CompanyOutcome]:
        return [outcome for outcome in self.outcomes if outcome.ok]

    def summary(self) -> PollSummary:
        notifications_emitted = self.notification.emitted if self.notification else 0
        notifications_failed = self.notification.failed if self.notification else 0
        return PollSummary.from_health(
            self.health_records,
            duration_ms=self.duration_ms,
            notifications_emitted=notifications_emitted,
            notifications_failed=notifications_failed,
        )

    def to_dict(self) -> dict[str, object]:
        return {"poll_id": self.poll_id, **self.summary().to_dict()}


def process_company(
    company: Company,
    *,
    settings: Settings,
    repository: JobRepository,
    job_filter: JobFilter | None = None,
    client: HttpClient | None = None,
    source: JobSource | None = None,
    now: datetime | None = None,
) -> CompanyOutcome:
    """Fetch, filter, fingerprint and persist one company's postings.

    Never raises. Whatever goes wrong becomes a health record, because a polling
    run covering 150 companies must not be abortable by any one of them.
    """
    timestamp = now or utcnow()
    started = time.perf_counter()
    job_filter = job_filter or JobFilter(settings.filters)

    try:
        source = source or build_source(company, client)
    except Exception as exc:
        logger.warning("%s: cannot build adapter: %s", company.company, exc)
        return CompanyOutcome(
            company=company.company,
            health=ScraperHealth(
                company=company.company,
                provider=company.provider,
                status=ScraperStatus.FAILED,
                timestamp=timestamp,
                error_type=type(exc).__name__,
                error=str(exc),
                duration_ms=int((time.perf_counter() - started) * 1000),
            ),
        )

    result = safe_fetch(source)
    health = result.health
    assert health is not None

    outcome = CompanyOutcome(company=company.company, health=health)
    if not result.ok:
        logger.warning("%s: scraper %s (%s)", company.company, health.status.value, health.error)
        return outcome

    decisions = job_filter.evaluate_all(result.jobs, company)
    relevant = [decision for decision in decisions if decision.keep]

    for decision in relevant:
        try:
            upsert = repository.upsert(
                decision.job,
                relevance_score=decision.score,
                company=company,
                now=timestamp,
            )
        except Exception as exc:
            logger.exception("%s: failed to persist %s", company.company, decision.job.title)
            outcome.health = health.with_counts(
                relevant_jobs=len(relevant),
                new_jobs=len(outcome.new_records),
                updated_jobs=len(outcome.updated_records),
            )
            # Surface the storage failure so the message is retried / DLQ'd.
            raise StorageFailure(f"{company.company}: {exc}") from exc

        outcome.seen_records.append(upsert.record)
        if upsert.is_new:
            outcome.new_records.append(upsert.record)
        elif upsert.is_updated:
            outcome.updated_records.append(upsert.record)

    outcome.health = health.with_counts(
        relevant_jobs=len(relevant),
        new_jobs=len(outcome.new_records),
        updated_jobs=len(outcome.updated_records),
    )
    logger.info(
        "scrape complete",
        extra={"structured": {**outcome.health.to_dict(), "provider": company.provider}},
    )
    return outcome


class StorageFailure(RuntimeError):
    """Persistence failed for a company.

    Deliberately *not* swallowed: unlike a broken source, a failing database means
    the work did not happen, so the SQS message should be retried and eventually
    land in the DLQ rather than being quietly marked done.
    """


class PollRunner:
    """Runs a set of companies through the pipeline and sends the alerts."""

    def __init__(
        self,
        settings: Settings,
        *,
        repository: JobRepository,
        notifier: Notifier | None = None,
        health_repository: HealthRepository | None = None,
        job_filter: JobFilter | None = None,
        client: HttpClient | None = None,
        source_factory: object | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.notifier = notifier
        self.health_repository = health_repository
        self.job_filter = job_filter or JobFilter(settings.filters)
        self.client = client
        #: Optional ``(company) -> JobSource`` hook, used by tests and the local
        #: runner to substitute deterministic sources for real HTTP.
        self.source_factory = source_factory

    def run(
        self,
        companies: Iterable[Company],
        *,
        poll_id: str = "local",
        now: datetime | None = None,
        notify: bool = True,
    ) -> PollOutcome:
        started = time.perf_counter()
        timestamp = now or utcnow()
        outcome = PollOutcome(poll_id=poll_id)

        for company in companies:
            source = None
            if self.source_factory is not None:
                source = self.source_factory(company)  # type: ignore[operator]
                if source is None:
                    continue
            try:
                company_outcome = process_company(
                    company,
                    settings=self.settings,
                    repository=self.repository,
                    job_filter=self.job_filter,
                    client=self.client,
                    source=source,
                    now=timestamp,
                )
            except StorageFailure:
                raise
            outcome.outcomes.append(company_outcome)

        if self.health_repository is not None:
            self.health_repository.record_many(outcome.health_records)

        if notify and self.notifier is not None:
            notifiable = [
                record
                for company_outcome in outcome.outcomes
                for record in company_outcome.notifiable
            ]
            if notifiable:
                outcome.notification = self.notifier.notify(notifiable, now=timestamp)

        outcome.duration_ms = int((time.perf_counter() - started) * 1000)
        logger.info("poll complete", extra={"structured": outcome.to_dict()})
        return outcome


__all__: Sequence[str] = (
    "CompanyOutcome",
    "PollOutcome",
    "PollRunner",
    "StorageFailure",
    "process_company",
)
