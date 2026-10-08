"""Notification payloads (PRD §22, §30, §8-app).

One :class:`NotificationEvent` is one alert the user will receive. It carries the
records, the channel, and - critically - the deep link per job, because the
acceptance scenario requires that tapping a notification resolves to the right job
in the client.

The event is JSON round-trippable because it crosses a queue boundary: the worker
that discovers a job is not the process that sends the alert.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from jobmonitor.models.job import posting_kind
from jobmonitor.models.record import JobRecord

SCHEMA_VERSION = 1
"""Bumped if the payload shape changes. The client checks it, so an old client
degrades visibly instead of silently mis-rendering a new payload."""


class Channel(StrEnum):
    EMAIL = "email"
    PUSH = "push"


class Urgency(StrEnum):
    #: A high-priority company: alert on its own, immediately.
    IMMEDIATE = "immediate"
    #: Lower priority: grouped with the rest of this poll's findings.
    BATCHED = "batched"


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class JobAlert:
    """One job as it appears inside a notification."""

    job_id: str
    company: str
    title: str
    url: str
    deep_link: str
    location: str | None = None
    first_seen: str | None = None
    date_posted: str | None = None
    relevance_score: int = 0
    priority: str = "medium"
    description_preview: str | None = None
    #: ``internship``, ``event``, ``industry_event`` or ``program``. Optional in
    #: the payload (absent means an internship), so no schema bump was needed.
    kind: str = "internship"
    #: Signed link to this job's Apply kit, when the kit is configured (internships).
    kit_url: str | None = None

    @property
    def is_event(self) -> bool:
        return self.kind != "internship"

    @classmethod
    def from_record(
        cls,
        record: JobRecord,
        *,
        app_base_url: str,
        kit_link: Callable[[str], str] | None = None,
    ) -> JobAlert:
        preview = None
        if record.description:
            preview = (
                record.description
                if len(record.description) <= 280
                else record.description[:280].rstrip() + "..."
            )
        return cls(
            job_id=record.job_id,
            company=record.company,
            title=record.title,
            url=record.url,
            deep_link=record.deep_link(app_base_url),
            location=record.location,
            first_seen=record.first_seen.isoformat(),
            date_posted=record.date_posted.isoformat() if record.date_posted else None,
            relevance_score=record.relevance_score,
            priority=record.priority,
            description_preview=preview,
            kind=posting_kind(record.employment_type),
            kit_url=(
                kit_link(record.job_id)
                if kit_link and posting_kind(record.employment_type) == "internship"
                else None
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "company": self.company,
            "title": self.title,
            "url": self.url,
            "deep_link": self.deep_link,
            "location": self.location,
            "first_seen": self.first_seen,
            "date_posted": self.date_posted,
            "relevance_score": self.relevance_score,
            "priority": self.priority,
            "description_preview": self.description_preview,
            "kind": self.kind,
            "kit_url": self.kit_url,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> JobAlert:
        return cls(
            job_id=str(data["job_id"]),
            company=str(data.get("company", "")),
            title=str(data.get("title", "")),
            url=str(data.get("url", "")),
            deep_link=str(data.get("deep_link", "")),
            location=data.get("location"),
            first_seen=data.get("first_seen"),
            date_posted=data.get("date_posted"),
            relevance_score=int(data.get("relevance_score", 0) or 0),
            priority=str(data.get("priority", "medium")),
            description_preview=data.get("description_preview"),
            kind=str(data.get("kind") or "internship"),
            kit_url=data.get("kit_url") or None,
        )


@dataclass(frozen=True, slots=True)
class NotificationEvent:
    jobs: tuple[JobAlert, ...]
    urgency: Urgency = Urgency.BATCHED
    created_at: datetime = field(default_factory=utcnow)
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.jobs:
            raise ValueError("a notification event must carry at least one job")

    @property
    def job_ids(self) -> tuple[str, ...]:
        return tuple(alert.job_id for alert in self.jobs)

    @property
    def is_single(self) -> bool:
        return len(self.jobs) == 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "urgency": self.urgency.value,
            "created_at": self.created_at.isoformat(),
            "jobs": [alert.to_dict() for alert in self.jobs],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> NotificationEvent:
        raw_jobs = data.get("jobs") or []
        if not isinstance(raw_jobs, list) or not raw_jobs:
            raise ValueError("notification payload has no jobs")
        created = data.get("created_at")
        try:
            created_at = (
                datetime.fromisoformat(str(created).replace("Z", "+00:00")) if created else utcnow()
            )
        except ValueError:
            created_at = utcnow()
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        try:
            urgency = Urgency(str(data.get("urgency", Urgency.BATCHED.value)))
        except ValueError:
            urgency = Urgency.BATCHED
        return cls(
            jobs=tuple(JobAlert.from_dict(entry) for entry in raw_jobs),
            urgency=urgency,
            created_at=created_at,
            schema_version=int(data.get("schema_version", SCHEMA_VERSION) or SCHEMA_VERSION),
        )

    @classmethod
    def from_json(cls, payload: str | bytes) -> NotificationEvent:
        return cls.from_dict(json.loads(payload))

    @classmethod
    def for_records(
        cls,
        records: Sequence[JobRecord],
        *,
        app_base_url: str,
        urgency: Urgency = Urgency.BATCHED,
        kit_link: Callable[[str], str] | None = None,
    ) -> NotificationEvent:
        return cls(
            jobs=tuple(
                JobAlert.from_record(r, app_base_url=app_base_url, kit_link=kit_link)
                for r in records
            ),
            urgency=urgency,
        )


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    channel: Channel
    delivered: bool
    job_ids: tuple[str, ...] = ()
    detail: str = ""
    error_type: str | None = None

    @classmethod
    def ok(cls, channel: Channel, job_ids: Sequence[str], detail: str = "") -> DeliveryResult:
        return cls(channel=channel, delivered=True, job_ids=tuple(job_ids), detail=detail)

    @classmethod
    def failed(
        cls, channel: Channel, job_ids: Sequence[str], error: BaseException
    ) -> DeliveryResult:
        return cls(
            channel=channel,
            delivered=False,
            job_ids=tuple(job_ids),
            detail=str(error),
            error_type=type(error).__name__,
        )


__all__: Sequence[str] = (
    "SCHEMA_VERSION",
    "Channel",
    "DeliveryResult",
    "JobAlert",
    "NotificationEvent",
    "Urgency",
)
