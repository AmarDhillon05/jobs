"""DynamoDB repositories (PRD §10).

Table design
------------

``jobs`` - partition key ``job_id``, plus three GSIs:

======================  ========================  ==============================
index                   keys                      answers
======================  ========================  ==============================
``recent-index``        ``feed`` / ``first_seen``  "what's new?" for the feed
``company-index``       ``company`` / ``first_seen``  "everything from Stripe"
``pending-index``       ``notification_pending`` / ``first_seen``  "what still needs alerting?"
======================  ========================  ==============================

``pending-index`` is deliberately **sparse**: ``notification_pending`` is written
when a record is created and *removed* when it is notified, so the index holds
only outstanding work instead of every job ever discovered.

``recent-index`` uses a constant partition key (``feed = "JOB"``). For one user
and ~150 companies that is a few hundred writes a day into one logical partition,
comfortably inside DynamoDB's per-partition limits, and it makes the feed a single
query. The date-bucketed alternative is documented in ARCHITECTURE.md.

Idempotent upsert
-----------------

One ``UpdateItem`` per sighting, no read-then-write:

* ``first_seen = if_not_exists(first_seen, :now)`` - immovable once set, so
  detection timing cannot drift, even under a concurrent retry.
* ``last_seen = :now`` and ``times_seen = if_not_exists(times_seen, :zero) + :one``.
* ``notification_sent = if_not_exists(notification_sent, :false)`` - a job already
  alerted about can never be reset to un-alerted by a later sighting.
* ``ReturnValues="ALL_OLD"`` tells us in the same round trip whether the item
  existed (new vs. seen-again) and what its previous ``content_hash`` was
  (seen-again vs. changed).

The sparse index key is *not* part of that expression, on purpose.
``if_not_exists(notification_pending, :pending)`` would look right and be wrong:
it re-adds the marker whenever an already-notified job is seen again (the marker
having been removed at notify time), putting the job back on the pending queue
and alerting the user a second time. Instead the marker is written by a small
follow-up call made only when ``ALL_OLD`` came back empty - i.e. only for
genuinely new jobs, a handful per poll - guarded so it can never apply to a
record that has already been notified. Steady-state sightings stay one request.

That makes "no duplicate notifications" a property of the storage layer rather
than something every caller has to remember.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any

from jobmonitor.config import AwsSettings
from jobmonitor.models.company import Company
from jobmonitor.models.health import ScraperHealth
from jobmonitor.models.job import Job
from jobmonitor.models.record import (
    FEED_PARTITION,
    PENDING_MARKER,
    DeviceRegistration,
    JobRecord,
    UpsertResult,
    utcnow,
)
from jobmonitor.storage.base import DeviceRepository, HealthRepository, JobRepository

RECENT_INDEX = "recent-index"
COMPANY_INDEX = "company-index"
PENDING_INDEX = "pending-index"

#: Attributes refreshed on every sighting. Kept as data so the update expression
#: and the reserved-word escaping stay in one place.
_REFRESHED_ATTRIBUTES: tuple[str, ...] = (
    "company",
    "title",
    "url",
    "source",
    "content_hash",
    "location",
    "external_id",
    "date_posted",
    "description",
    "employment_type",
    "relevance_score",
    "priority",
    "industry",
    "feed",
)


def build_resource(settings: AwsSettings, *, resource: Any = None) -> Any:
    """A DynamoDB service resource, honouring ``AWS_ENDPOINT_URL`` for LocalStack."""
    if resource is not None:
        return resource
    import boto3

    kwargs: dict[str, Any] = {"region_name": settings.region}
    if settings.endpoint_url:
        kwargs["endpoint_url"] = settings.endpoint_url
    return boto3.resource("dynamodb", **kwargs)


class DynamoJobRepository(JobRepository):
    def __init__(self, settings: AwsSettings, *, resource: Any = None) -> None:
        self.settings = settings
        self._resource = build_resource(settings, resource=resource)
        self._table = self._resource.Table(settings.jobs_table)

    @property
    def table(self) -> Any:
        return self._table

    # ------------------------------------------------------------------ writing
    def upsert(
        self,
        job: Job,
        *,
        relevance_score: int = 0,
        company: Company | None = None,
        now: datetime | None = None,
    ) -> UpsertResult:
        timestamp = now or utcnow()
        candidate = JobRecord.from_job(
            job, relevance_score=relevance_score, company=company, now=timestamp
        )
        item = candidate.to_item()

        set_parts = [
            "first_seen = if_not_exists(first_seen, :first_seen)",
            "last_seen = :last_seen",
            "times_seen = if_not_exists(times_seen, :zero) + :one",
            "notification_sent = if_not_exists(notification_sent, :false)",
        ]
        names: dict[str, str] = {}
        values: dict[str, Any] = {
            ":first_seen": item["first_seen"],
            ":last_seen": item["last_seen"],
            ":zero": 0,
            ":one": 1,
            ":false": False,
        }
        removes: list[str] = []

        for attribute in _REFRESHED_ATTRIBUTES:
            placeholder_name = f"#{attribute}"
            placeholder_value = f":{attribute}"
            names[placeholder_name] = attribute
            if attribute in item:
                set_parts.append(f"{placeholder_name} = {placeholder_value}")
                values[placeholder_value] = item[attribute]
            else:
                # The source stopped providing this field: clear it rather than
                # leave a stale value behind.
                removes.append(placeholder_name)

        expression = "SET " + ", ".join(set_parts)
        if removes:
            expression += " REMOVE " + ", ".join(removes)

        response = self._table.update_item(
            Key={"job_id": candidate.job_id},
            UpdateExpression=expression,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ReturnValues="ALL_OLD",
        )
        previous: Mapping[str, Any] = response.get("Attributes") or {}

        if not previous:
            self._mark_pending(candidate.job_id)
            return UpsertResult(record=candidate, is_new=True, is_updated=False)

        existing = JobRecord.from_item(previous)
        stored = JobRecord.from_item(
            {
                **previous,
                **item,
                "first_seen": existing.first_seen.isoformat(),
                "last_seen": item["last_seen"],
                "times_seen": existing.times_seen + 1,
                "notification_sent": existing.notification_sent,
                **(
                    {"notified_at": existing.notified_at.isoformat()}
                    if existing.notified_at
                    else {}
                ),
                **(
                    {"notification_pending": existing.notification_pending}
                    if existing.notification_pending
                    else {}
                ),
            }
        )
        return UpsertResult(
            record=stored,
            is_new=False,
            is_updated=existing.content_hash != candidate.content_hash,
        )

    def _mark_pending(self, job_id: str) -> None:
        """Add the sparse-index marker for a newly created record.

        Guarded on ``notification_sent = false`` so a race with the notifier can
        never resurrect an alert that has already gone out. A crash between the
        create and this call leaves the job stored but off the pending index; the
        index is a backstop (the worker emits the notification from its own
        UpsertResult), so the cost is a missed retry, not a missed job.
        """
        from botocore.exceptions import ClientError

        try:
            self._table.update_item(
                Key={"job_id": job_id},
                UpdateExpression="SET notification_pending = :pending",
                ConditionExpression="notification_sent = :false",
                ExpressionAttributeValues={":pending": PENDING_MARKER, ":false": False},
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise

    def mark_notified(self, job_ids: Iterable[str], *, now: datetime | None = None) -> int:
        from botocore.exceptions import ClientError

        timestamp = (now or utcnow()).isoformat()
        changed = 0
        for job_id in job_ids:
            try:
                self._table.update_item(
                    Key={"job_id": job_id},
                    UpdateExpression=(
                        "SET notification_sent = :true, notified_at = :at "
                        "REMOVE notification_pending"
                    ),
                    # Both guards matter: attribute_exists stops us resurrecting a
                    # deleted job, and notification_sent = :false makes a
                    # redelivered queue message a no-op instead of a second alert.
                    ConditionExpression=("attribute_exists(job_id) AND notification_sent = :false"),
                    ExpressionAttributeValues={":true": True, ":false": False, ":at": timestamp},
                )
                changed += 1
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                    raise
        return changed

    # ------------------------------------------------------------------ reading
    def get(self, job_id: str) -> JobRecord | None:
        response = self._table.get_item(Key={"job_id": job_id})
        item = response.get("Item")
        return JobRecord.from_item(item) if item else None

    def recent(self, *, limit: int = 50, since: datetime | None = None) -> list[JobRecord]:
        from boto3.dynamodb.conditions import Key

        condition = Key("feed").eq(FEED_PARTITION)
        if since is not None:
            condition = condition & Key("first_seen").gte(since.isoformat())
        response = self._table.query(
            IndexName=RECENT_INDEX,
            KeyConditionExpression=condition,
            ScanIndexForward=False,  # newest first
            Limit=max(1, limit),
        )
        return [JobRecord.from_item(item) for item in response.get("Items", [])]

    def by_company(self, company: str, *, limit: int = 50) -> list[JobRecord]:
        from boto3.dynamodb.conditions import Key

        response = self._table.query(
            IndexName=COMPANY_INDEX,
            KeyConditionExpression=Key("company").eq(company),
            ScanIndexForward=False,
            Limit=max(1, limit),
        )
        return [JobRecord.from_item(item) for item in response.get("Items", [])]

    def pending_notifications(self, *, limit: int = 100) -> list[JobRecord]:
        from boto3.dynamodb.conditions import Key

        response = self._table.query(
            IndexName=PENDING_INDEX,
            KeyConditionExpression=Key("notification_pending").eq(PENDING_MARKER),
            ScanIndexForward=True,  # oldest outstanding first
            Limit=max(1, limit),
        )
        return [JobRecord.from_item(item) for item in response.get("Items", [])]

    def count(self) -> int:
        response = self._table.scan(Select="COUNT")
        return int(response.get("Count", 0))


class DynamoHealthRepository(HealthRepository):
    """Latest health per company/provider, with a TTL so it self-cleans (PRD §10)."""

    #: Health rows expire after a week; they are diagnostics, not records.
    TTL_SECONDS = 7 * 24 * 60 * 60

    def __init__(self, settings: AwsSettings, *, resource: Any = None) -> None:
        self.settings = settings
        self._resource = build_resource(settings, resource=resource)
        self._table = self._resource.Table(settings.health_table)

    @property
    def table(self) -> Any:
        return self._table

    def record(self, health: ScraperHealth) -> None:
        item = {key: value for key, value in health.to_dict().items() if value is not None}
        item["scraper_key"] = health.key
        item["expires_at"] = int(health.timestamp.timestamp()) + self.TTL_SECONDS
        self._table.put_item(Item=item)

    def latest(self) -> list[ScraperHealth]:
        # A full scan is correct here: the table holds one row per monitored
        # company (~150 rows), so this is a single sub-kilobyte page.
        response = self._table.scan()
        records = [ScraperHealth.from_dict(item) for item in response.get("Items", [])]
        records.sort(key=lambda health: (health.timestamp, health.company), reverse=True)
        return records

    def for_company(self, company: str) -> ScraperHealth | None:
        folded = company.casefold()
        for health in self.latest():
            if health.company.casefold() == folded:
                return health
        return None


class DynamoDeviceRepository(DeviceRepository):
    def __init__(self, settings: AwsSettings, *, resource: Any = None) -> None:
        self.settings = settings
        self._resource = build_resource(settings, resource=resource)
        self._table = self._resource.Table(settings.devices_table)

    @property
    def table(self) -> Any:
        return self._table

    def register(self, device: DeviceRegistration) -> DeviceRegistration:
        existing = self._get(device.device_id)
        if existing is not None:
            from dataclasses import replace

            device = replace(device, registered_at=existing.registered_at)
        self._table.put_item(Item=device.to_item())
        return device

    def unregister(self, device_id: str) -> bool:
        response = self._table.delete_item(Key={"device_id": device_id}, ReturnValues="ALL_OLD")
        return bool(response.get("Attributes"))

    def enabled_devices(self) -> list[DeviceRegistration]:
        response = self._table.scan()
        devices = [DeviceRegistration.from_item(item) for item in response.get("Items", [])]
        return [device for device in devices if device.enabled]

    def _get(self, device_id: str) -> DeviceRegistration | None:
        item = self._table.get_item(Key={"device_id": device_id}).get("Item")
        return DeviceRegistration.from_item(item) if item else None


__all__: Sequence[str] = (
    "COMPANY_INDEX",
    "PENDING_INDEX",
    "RECENT_INDEX",
    "DynamoDeviceRepository",
    "DynamoHealthRepository",
    "DynamoJobRepository",
    "build_resource",
)
