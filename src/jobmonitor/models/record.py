"""The persisted job record (PRD §9).

A :class:`Job` is what a board said. A :class:`JobRecord` is what we know about
it over time - crucially ``first_seen``, which is authoritative for detection
timing because careers sites' own posting timestamps are unreliable or absent.

``content_hash`` separates "seen again" from "changed", and ``notification_sent``
is what makes the same unchanged posting unable to alert twice.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from jobmonitor.identity import content_hash, job_id
from jobmonitor.models.company import Company, Priority
from jobmonitor.models.job import Job

#: Constant partition key for the "recent jobs" index. One user and ~150
#: companies means a single logical partition is fine; the alternative (a
#: date-bucketed key) is documented in ARCHITECTURE.md as the scaling path.
FEED_PARTITION = "JOB"

#: Written only while a notification is outstanding, then removed. That makes the
#: pending-notification GSI *sparse*: it holds the handful of un-notified jobs
#: rather than every job ever seen.
PENDING_MARKER = "pending"


def utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _parse(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class JobRecord:
    job_id: str
    company: str
    title: str
    url: str
    source: str
    content_hash: str
    first_seen: datetime
    last_seen: datetime
    location: str | None = None
    external_id: str | None = None
    date_posted: datetime | None = None
    description: str | None = None
    employment_type: str | None = None
    relevance_score: int = 0
    notification_sent: bool = False
    notified_at: datetime | None = None
    times_seen: int = 1
    priority: str = Priority.MEDIUM.value
    industry: str = "unknown"
    feed: str = FEED_PARTITION
    #: Present only while a notification is outstanding (sparse-GSI key).
    notification_pending: str | None = PENDING_MARKER

    # ------------------------------------------------------------- construction
    @classmethod
    def from_job(
        cls,
        job: Job,
        *,
        relevance_score: int = 0,
        company: Company | None = None,
        now: datetime | None = None,
    ) -> JobRecord:
        timestamp = now or utcnow()
        return cls(
            job_id=job_id(job),
            company=job.company,
            title=job.title,
            url=job.url,
            source=job.source,
            content_hash=content_hash(job),
            first_seen=timestamp,
            last_seen=timestamp,
            location=job.location,
            external_id=job.external_id,
            date_posted=job.date_posted,
            description=job.description,
            employment_type=job.employment_type,
            relevance_score=relevance_score,
            priority=(company.priority.value if company else Priority.MEDIUM.value),
            industry=(company.industry if company else "unknown"),
        )

    # ------------------------------------------------------------------ helpers
    @property
    def age_seconds(self) -> float:
        return (utcnow() - self.first_seen).total_seconds()

    @property
    def is_pending_notification(self) -> bool:
        return not self.notification_sent

    def deep_link(self, app_base_url: str) -> str:
        """The URL a notification tap should open in the client."""
        from urllib.parse import quote

        return f"{app_base_url.rstrip('/')}/jobs/{quote(self.job_id, safe='')}"

    def marked_notified(self, now: datetime | None = None) -> JobRecord:
        return replace(
            self,
            notification_sent=True,
            notified_at=now or utcnow(),
            notification_pending=None,
        )

    def seen_again(
        self,
        job: Job,
        *,
        now: datetime | None = None,
        relevance_score: int | None = None,
    ) -> JobRecord:
        """A later sighting of the same posting: refresh content, keep first_seen."""
        timestamp = now or utcnow()
        return replace(
            self,
            title=job.title,
            url=job.url,
            location=job.location,
            description=job.description,
            employment_type=job.employment_type,
            date_posted=job.date_posted,
            content_hash=content_hash(job),
            last_seen=timestamp,
            times_seen=self.times_seen + 1,
            relevance_score=(self.relevance_score if relevance_score is None else relevance_score),
        )

    # ------------------------------------------------------------ serialisation
    def to_item(self) -> dict[str, Any]:
        """DynamoDB item. ``None`` values are omitted, not stored as NULL."""
        item: dict[str, Any] = {
            "job_id": self.job_id,
            "company": self.company,
            "title": self.title,
            "url": self.url,
            "source": self.source,
            "content_hash": self.content_hash,
            "first_seen": self.first_seen.isoformat(),
            "last_seen": self.last_seen.isoformat(),
            "relevance_score": int(self.relevance_score),
            "notification_sent": bool(self.notification_sent),
            "times_seen": int(self.times_seen),
            "priority": self.priority,
            "industry": self.industry,
            "feed": self.feed,
        }
        optional = {
            "location": self.location,
            "external_id": self.external_id,
            "date_posted": _iso(self.date_posted),
            "description": self.description,
            "employment_type": self.employment_type,
            "notified_at": _iso(self.notified_at),
            "notification_pending": self.notification_pending,
        }
        item.update({key: value for key, value in optional.items() if value is not None})
        return item

    @classmethod
    def from_item(cls, item: Mapping[str, Any]) -> JobRecord:
        first_seen = _parse(item.get("first_seen")) or utcnow()
        return cls(
            job_id=str(item["job_id"]),
            company=str(item.get("company", "")),
            title=str(item.get("title", "")),
            url=str(item.get("url", "")),
            source=str(item.get("source", "")),
            content_hash=str(item.get("content_hash", "")),
            first_seen=first_seen,
            last_seen=_parse(item.get("last_seen")) or first_seen,
            location=item.get("location"),
            external_id=item.get("external_id"),
            date_posted=_parse(item.get("date_posted")),
            description=item.get("description"),
            employment_type=item.get("employment_type"),
            relevance_score=int(item.get("relevance_score", 0) or 0),
            notification_sent=bool(item.get("notification_sent", False)),
            notified_at=_parse(item.get("notified_at")),
            times_seen=int(item.get("times_seen", 1) or 1),
            priority=str(item.get("priority", Priority.MEDIUM.value)),
            industry=str(item.get("industry", "unknown")),
            feed=str(item.get("feed", FEED_PARTITION)),
            notification_pending=item.get("notification_pending"),
        )

    def to_api_dict(self) -> dict[str, Any]:
        """The shape the client consumes. No internal bookkeeping leaks out."""
        return {
            "job_id": self.job_id,
            "company": self.company,
            "title": self.title,
            "location": self.location,
            "url": self.url,
            "source": self.source,
            "date_posted": _iso(self.date_posted),
            "first_seen": _iso(self.first_seen),
            "last_seen": _iso(self.last_seen),
            "relevance_score": self.relevance_score,
            "priority": self.priority,
            "industry": self.industry,
            "employment_type": self.employment_type,
            "description": self.description,
            "notification_sent": self.notification_sent,
        }


@dataclass(frozen=True, slots=True)
class UpsertResult:
    """What an idempotent write actually did."""

    record: JobRecord
    is_new: bool
    is_updated: bool

    @property
    def should_notify(self) -> bool:
        """Only genuinely new postings notify.

        An *updated* posting deliberately does not re-notify: PRD §9 requires the
        same job never to alert twice, and a recruiter re-wording a description
        is not news. The update is still recorded and still visible in the feed.
        """
        return self.is_new and not self.record.notification_sent


@dataclass(frozen=True, slots=True)
class DeviceRegistration:
    """A client that wants push notifications (PRD §24 POST /devices/register)."""

    device_id: str
    #: Opaque, transport-specific address (Web Push subscription, FCM token, ...).
    token: str
    transport: str = "webpush"
    registered_at: datetime = field(default_factory=utcnow)
    last_seen: datetime = field(default_factory=utcnow)
    enabled: bool = True
    label: str | None = None

    def to_item(self) -> dict[str, Any]:
        item = {
            "device_id": self.device_id,
            "token": self.token,
            "transport": self.transport,
            "registered_at": self.registered_at.isoformat(),
            "last_seen": self.last_seen.isoformat(),
            "enabled": bool(self.enabled),
        }
        if self.label:
            item["label"] = self.label
        return item

    @classmethod
    def from_item(cls, item: Mapping[str, Any]) -> DeviceRegistration:
        registered = _parse(item.get("registered_at")) or utcnow()
        return cls(
            device_id=str(item["device_id"]),
            token=str(item.get("token", "")),
            transport=str(item.get("transport", "webpush")),
            registered_at=registered,
            last_seen=_parse(item.get("last_seen")) or registered,
            enabled=bool(item.get("enabled", True)),
            label=item.get("label"),
        )


__all__: Sequence[str] = (
    "FEED_PARTITION",
    "PENDING_MARKER",
    "DeviceRegistration",
    "JobRecord",
    "UpsertResult",
    "utcnow",
)
