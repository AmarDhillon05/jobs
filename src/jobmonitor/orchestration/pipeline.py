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
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from jobmonitor.config import Settings
from jobmonitor.filtering import JobFilter
from jobmonitor.filtering.location import split_by_location
from jobmonitor.filtering.programs import tag_programs
from jobmonitor.filtering.recency import split_by_recency
from jobmonitor.http import HttpClient
from jobmonitor.models.company import Company, EventCategory
from jobmonitor.models.health import PollSummary, ScraperHealth, ScraperStatus, utcnow
from jobmonitor.models.job import EVENT_TYPE, INDUSTRY_EVENT_TYPE, Job, is_event_kind
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
    #: True for the first successful poll of a new event source: everything it
    #: found was already listed before monitoring began, so it is stored silently.
    quiet: bool = False

    @property
    def ok(self) -> bool:
        return self.health.ok

    @property
    def notifiable(self) -> list[JobRecord]:
        if self.quiet:
            return []
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
    quiet: bool = False,
) -> CompanyOutcome:
    """Fetch, filter, fingerprint and persist one company's postings.

    Never raises. Whatever goes wrong becomes a health record, because a polling
    run covering 150 companies must not be abortable by any one of them.

    ``quiet`` stores new records already marked as alerted (``baseline``), so
    they reach neither push nor the digest: the first poll of a new event source.
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

    jobs = result.jobs
    if company.event_category is not None:
        # Everything an event source lists is an event; its category decides which
        # label. An adapter that already knows better (a program) keeps its tag.
        jobs = [_as_event(job, company.event_category) for job in jobs]
        recent = jobs  # an event's date is when it happens, not when it was posted
    else:
        # Only postings from the last day are ever seen (FilterSettings.max_posting_age).
        # Applied here, once, so every adapter obeys the same rule.
        recent = split_by_recency(
            jobs,
            now=timestamp,
            max_age=settings.filters.max_posting_age,
            keep_undated=settings.filters.keep_undated,
        ).recent
        # Student programs posted as jobs (fellowships, discovery programs...).
        recent = tag_programs(recent)
    # ... and only postings in the US (FilterSettings.us_only). Anything else is
    # never scored, stored or alerted on.
    placed = split_by_location(
        recent,
        us_only=settings.filters.us_only,
        keep_unknown=settings.filters.keep_unknown_locations,
    )
    if placed.outside_us:
        logger.info(
            "%s: skipped %d posting(s) outside the US", company.company, len(placed.outside_us)
        )
    decisions = job_filter.evaluate_all(placed.kept, company)
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

    if quiet and outcome.new_records:
        repository.mark_notified(
            [record.job_id for record in outcome.new_records], now=timestamp, baseline=True
        )
        outcome.quiet = True
        logger.info(
            "%s: first poll of %s; stored %d existing event(s) without alerting",
            company.company,
            company.provider,
            len(outcome.new_records),
        )

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


def _as_event(job: Job, category: EventCategory) -> Job:
    if is_event_kind(job.employment_type):
        return job
    label = EVENT_TYPE if category is EventCategory.RECRUITING else INDUSTRY_EVENT_TYPE
    return job.with_fields(employment_type=label)


class StorageFailure(RuntimeError):
    """Persistence failed for a company.

    Deliberately *not* swallowed: unlike a broken source, a failing database means
    the work did not happen, so the SQS message should be retried and eventually
    land in the DLQ rather than being quietly marked done.
    """


#: An event source failing this many polls in a row is polled at most once per
#: :data:`EVENT_SOURCE_COOLDOWN` until it recovers, instead of every poll.
EVENT_SOURCE_FAILURE_LIMIT = 3
EVENT_SOURCE_COOLDOWN = timedelta(hours=1)


class PollRunner:
    """Runs a set of companies through the pipeline and sends the alerts.

    Each company is expanded into its sources (:meth:`Company.sources`): the job
    board, then every event source. Each source is processed on its own, so a
    broken events page never affects the job board, and vice versa.
    """

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
        quiet_first_event_poll: bool = True,
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
        #: Store the first successful poll of a new event source silently. Needs a
        #: health repository to know what "new" means; without one, nothing is quiet.
        self.quiet_first_event_poll = quiet_first_event_poll

    def _previous_health(self, target: Company) -> ScraperHealth | None:
        if self.health_repository is None or not target.is_event_source:
            return None
        try:
            return self.health_repository.get(target.key)
        except Exception:  # health is diagnostics: never let it stop a poll
            logger.exception("%s: could not read previous health", target.key)
            return None

    @staticmethod
    def _cooling_down(previous: ScraperHealth | None, now: datetime) -> bool:
        return bool(
            previous
            and previous.consecutive_failures >= EVENT_SOURCE_FAILURE_LIMIT
            and now - previous.timestamp < EVENT_SOURCE_COOLDOWN
        )

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
            for target in company.sources():
                previous = self._previous_health(target)
                if target.is_event_source and self._cooling_down(previous, timestamp):
                    logger.info(
                        "%s: skipped this poll after %d failures in a row; retrying hourly",
                        target.key,
                        previous.consecutive_failures if previous else 0,
                    )
                    continue
                source = None
                if self.source_factory is not None:
                    source = self.source_factory(target)  # type: ignore[operator]
                    if source is None:
                        continue
                quiet = (
                    target.is_event_source
                    and self.quiet_first_event_poll
                    and self.health_repository is not None
                    and not (previous and (previous.ever_succeeded or previous.ok))
                )
                company_outcome = process_company(
                    target,
                    settings=self.settings,
                    repository=self.repository,
                    job_filter=self.job_filter,
                    client=self.client,
                    source=source,
                    now=timestamp,
                    quiet=quiet,
                )
                if target.is_event_source:
                    # Stamped with the poll's time, which the cool-down compares against.
                    company_outcome.health = replace(
                        company_outcome.health, provider=target.health_provider, timestamp=timestamp
                    ).carried_from(previous)
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
