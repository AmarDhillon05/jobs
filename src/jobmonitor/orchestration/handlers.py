"""Lambda entry points.

Three handlers make up the polling architecture:

``coordinator_handler``
    Triggered by the EventBridge schedule. Shards the registry and enqueues one
    SQS message per shard. Cheap and fast: it does no HTTP.

``worker_handler``
    Triggered by the scrape queue. Runs one shard through the pipeline and
    publishes a notification event for genuinely new jobs. Returns
    ``batchItemFailures`` so SQS retries **only** the shards that actually failed
    and lets the rest succeed - which is what routes a permanently broken shard to
    the DLQ without replaying its healthy siblings.

``notifier_handler``
    Triggered by the notification queue (fed by SNS). Renders and delivers, then
    marks the records notified. Marking is conditional in the repository, so a
    redelivery is a no-op rather than a second alert.

``digest_handler``
    Triggered hourly by its own schedule. Emails everything first seen in the
    last complete hour, or sends nothing when the hour found nothing.

Every handler is a thin shell: it parses its event, builds dependencies from
:class:`Settings`, calls into the pipeline, and returns a JSON-serialisable
summary. The behaviour lives in modules that are testable without Lambda.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from jobmonitor.config import Settings
from jobmonitor.filtering import JobFilter
from jobmonitor.http import HttpClient
from jobmonitor.models.company import CompanyRegistry, load_default_registry
from jobmonitor.notifications.digest import DigestSender
from jobmonitor.notifications.events import NotificationEvent
from jobmonitor.notifications.notifier import Notifier
from jobmonitor.notifications.transports import build_email_transport, build_push_transport
from jobmonitor.orchestration.messages import MessageError, ScheduledPollEvent, ScrapeTask
from jobmonitor.orchestration.pipeline import PollRunner, StorageFailure
from jobmonitor.orchestration.queues import (
    PublishError,
    QueuePublisher,
    SnsTopic,
    SqsQueue,
    TopicPublisher,
)
from jobmonitor.storage.base import DeviceRepository, HealthRepository, JobRepository
from jobmonitor.storage.dynamo import (
    DynamoDeviceRepository,
    DynamoHealthRepository,
    DynamoJobRepository,
)

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))


@dataclass
class Dependencies:
    """Everything a handler needs, built once per container (warm-start reuse)."""

    settings: Settings
    registry: CompanyRegistry
    repository: JobRepository
    health: HealthRepository
    devices: DeviceRepository
    scrape_queue: QueuePublisher
    notification_topic: TopicPublisher

    @classmethod
    def from_env(cls, *, registry_path: str | None = None) -> Dependencies:
        settings = Settings.from_env()
        aws = settings.aws
        return cls(
            settings=settings,
            registry=load_default_registry(registry_path),
            repository=DynamoJobRepository(aws),
            health=DynamoHealthRepository(aws),
            devices=DynamoDeviceRepository(aws),
            scrape_queue=SqsQueue(
                aws.scrape_queue_url, region=aws.region, endpoint_url=aws.endpoint_url
            ),
            notification_topic=SnsTopic(
                aws.notification_topic_arn, region=aws.region, endpoint_url=aws.endpoint_url
            ),
        )

    def notifier(self) -> Notifier:
        aws = self.settings.aws
        return Notifier(
            self.settings,
            repository=self.repository,
            email_transport=build_email_transport(
                self.settings.email, region=aws.region, endpoint_url=aws.endpoint_url
            ),
            push_transport=build_push_transport(
                self.settings.push,
                topic_arn=aws.notification_topic_arn,
                region=aws.region,
                endpoint_url=aws.endpoint_url,
                # Lets the Expo transport retire a token Expo reports as gone.
                device_repository=self.devices,
            ),
            device_repository=self.devices,
        )

    def digest_sender(self) -> DigestSender:
        aws = self.settings.aws
        return DigestSender(
            self.settings,
            repository=self.repository,
            email_transport=build_email_transport(
                self.settings.email, region=aws.region, endpoint_url=aws.endpoint_url
            ),
        )


#: Reused across warm invocations; ``None`` until the first call.
_CACHED: Dependencies | None = None


def get_dependencies(override: Dependencies | None = None) -> Dependencies:
    global _CACHED
    if override is not None:
        return override
    if _CACHED is None:
        _CACHED = Dependencies.from_env()
    return _CACHED


def reset_dependencies() -> None:
    """Drop the warm-start cache. Used by tests between scenarios."""
    global _CACHED
    _CACHED = None


# ------------------------------------------------------------------ coordinator


def coordinator_handler(
    event: Mapping[str, Any] | None = None,
    context: Any = None,
    *,
    deps: Dependencies | None = None,
) -> dict[str, Any]:
    """EventBridge schedule -> SQS shards."""
    dependencies = get_dependencies(deps)
    poll = ScheduledPollEvent.from_dict(event)

    registry = dependencies.registry
    if poll.only_companies:
        wanted = {name.casefold() for name in poll.only_companies}
        registry = CompanyRegistry(tuple(c for c in registry if c.company.casefold() in wanted))
    if poll.only_provider:
        registry = CompanyRegistry(tuple(registry.by_provider(poll.only_provider)))

    tasks = ScrapeTask.shard(registry, poll_id=poll.poll_id, size=dependencies.settings.shard_size)
    summary: dict[str, Any] = {
        "poll_id": poll.poll_id,
        "companies": len(registry.pollable()),
        "shards": len(tasks),
        "shard_size": dependencies.settings.shard_size,
        "dry_run": poll.dry_run,
    }

    if poll.dry_run or not tasks:
        logger.info("coordinator: %s", json.dumps(summary))
        return {**summary, "published": 0}

    result = dependencies.scrape_queue.publish([task.to_json() for task in tasks])
    summary["published"] = result.count
    summary["publish_failures"] = [list(failure) for failure in result.failed]
    logger.info("coordinator: %s", json.dumps(summary))

    if result.failed:
        # Unpublished shards never run, so this must be loud, not a log line.
        raise PublishError(
            f"{len(result.failed)} of {len(tasks)} shard(s) failed to publish: {result.failed[:3]}"
        )
    return summary


# ----------------------------------------------------------------------- worker


def _sqs_records(event: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    records = (event or {}).get("Records") or []
    return [record for record in records if isinstance(record, Mapping)]


def worker_handler(
    event: Mapping[str, Any] | None = None,
    context: Any = None,
    *,
    deps: Dependencies | None = None,
) -> dict[str, Any]:
    """SQS scrape queue -> pipeline -> DynamoDB -> notification event."""
    dependencies = get_dependencies(deps)
    settings = dependencies.settings
    client = HttpClient(settings.http)
    runner = PollRunner(
        settings,
        repository=dependencies.repository,
        health_repository=dependencies.health,
        job_filter=JobFilter(settings.filters),
        client=client,
    )

    batch_item_failures: list[dict[str, str]] = []
    processed = 0
    new_jobs = 0
    failed_scrapers = 0

    for record in _sqs_records(event):
        message_id = str(record.get("messageId", ""))
        try:
            task = ScrapeTask.from_json(record.get("body") or "{}")
        except MessageError as exc:
            # Unparseable body: retrying cannot help, so let it go to the DLQ by
            # reporting it as failed rather than looping on it forever.
            logger.error("worker: undecodable message %s: %s", message_id, exc)
            batch_item_failures.append({"itemIdentifier": message_id})
            continue

        try:
            outcome = runner.run(task.companies, poll_id=task.poll_id, notify=False)
        except StorageFailure as exc:
            # The work genuinely did not happen: retry the shard.
            logger.error("worker: storage failure on shard %s: %s", task.shard_index, exc)
            batch_item_failures.append({"itemIdentifier": message_id})
            continue

        processed += len(task.companies)
        failed_scrapers += len(outcome.failures)

        notifiable = [
            job for company_outcome in outcome.outcomes for job in company_outcome.notifiable
        ]
        if notifiable:
            event_payload = NotificationEvent.for_records(
                notifiable, app_base_url=settings.app_base_url
            )
            try:
                dependencies.notification_topic.publish(
                    event_payload.to_json(), subject="New internships"
                )
                new_jobs += len(notifiable)
            except PublishError as exc:
                # The jobs are stored and still flagged pending, so the next poll
                # or a queue retry will alert. Retry this shard anyway: it is the
                # cheapest way to get the alert out promptly.
                logger.error("worker: could not publish notification event: %s", exc)
                batch_item_failures.append({"itemIdentifier": message_id})

    summary = {
        "companies_processed": processed,
        "scrapers_failed": failed_scrapers,
        "new_jobs": new_jobs,
        "batch_item_failures": len(batch_item_failures),
    }
    logger.info("worker: %s", json.dumps(summary))
    # The key name is fixed by the Lambda/SQS partial-batch-response contract.
    return {**summary, "batchItemFailures": batch_item_failures}


# --------------------------------------------------------------------- notifier


def _notification_payloads(event: Mapping[str, Any] | None) -> list[tuple[str, str]]:
    """(messageId, payload) pairs, unwrapping the SNS-inside-SQS envelope."""
    out: list[tuple[str, str]] = []
    for record in _sqs_records(event):
        message_id = str(record.get("messageId", ""))
        body = record.get("body") or ""
        try:
            parsed = json.loads(body)
        except (TypeError, json.JSONDecodeError):
            out.append((message_id, body))
            continue
        # An SNS -> SQS subscription wraps the payload in `Message`.
        if isinstance(parsed, Mapping) and "Message" in parsed and "jobs" not in parsed:
            out.append((message_id, str(parsed["Message"])))
        else:
            out.append((message_id, body))
    return out


def notifier_handler(
    event: Mapping[str, Any] | None = None,
    context: Any = None,
    *,
    deps: Dependencies | None = None,
) -> dict[str, Any]:
    """Notification queue -> email + push."""
    dependencies = get_dependencies(deps)
    notifier = dependencies.notifier()

    batch_item_failures: list[dict[str, str]] = []
    delivered = 0
    failed = 0
    suppressed = 0

    for message_id, payload in _notification_payloads(event):
        try:
            notification = NotificationEvent.from_json(payload)
        except (ValueError, TypeError) as exc:
            logger.error("notifier: undecodable payload %s: %s", message_id, exc)
            batch_item_failures.append({"itemIdentifier": message_id})
            continue

        # Re-read from storage: the payload says what *was* new, the records say
        # what has already been alerted about. Storage wins, which is what makes
        # a redelivered message silent.
        records = [
            record
            for record in (dependencies.repository.get(job_id) for job_id in notification.job_ids)
            if record is not None
        ]
        if not records:
            logger.warning("notifier: no stored records for %s", notification.job_ids[:3])
            continue

        outcome = notifier.notify(records)
        delivered += outcome.emitted
        failed += outcome.failed
        suppressed += len(outcome.skipped_job_ids)
        if outcome.events and outcome.emitted == 0:
            # Nothing got through: retry, then DLQ.
            batch_item_failures.append({"itemIdentifier": message_id})

    summary = {
        "notifications_emitted": delivered,
        "notifications_failed": failed,
        "suppressed_duplicates": suppressed,
        "batch_item_failures": len(batch_item_failures),
    }
    logger.info("notifier: %s", json.dumps(summary))
    return {**summary, "batchItemFailures": batch_item_failures}


# ----------------------------------------------------------------------- digest


def _event_time(event: Mapping[str, Any] | None) -> datetime:
    """The schedule's own timestamp (EventBridge ``time``), else now.

    Using the event's time rather than the clock makes a retried invocation
    cover the same window as the attempt it retries.
    """
    raw = (event or {}).get("time")
    if isinstance(raw, str) and raw:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            logger.warning("digest: unreadable event time %r; using now", raw)
        else:
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return datetime.now(UTC)


def digest_handler(
    event: Mapping[str, Any] | None = None,
    context: Any = None,
    *,
    deps: Dependencies | None = None,
) -> dict[str, Any]:
    """Hourly schedule -> one summary email, or none for an empty hour."""
    dependencies = get_dependencies(deps)
    outcome = dependencies.digest_sender().send(now=_event_time(event))
    summary = outcome.to_dict()
    logger.info("digest: %s", json.dumps(summary))
    return summary


__all__: Sequence[str] = (
    "Dependencies",
    "coordinator_handler",
    "digest_handler",
    "get_dependencies",
    "notifier_handler",
    "reset_dependencies",
    "worker_handler",
)
