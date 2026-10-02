"""Scraper health records (PRD §16, §25).

One record per company per poll. Persisted so the CLI/health view can answer
"which sources are broken right now, and why" without reading logs.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class ScraperStatus(StrEnum):
    SUCCESS = "success"
    #: Fetched fine, found no *internship* postings. Explicitly not a failure:
    #: most boards have no open internships most of the time (PRD §7).
    EMPTY = "empty"
    #: Fetched, but some individual records were unparseable and were skipped.
    DEGRADED = "degraded"
    FAILED = "failed"
    #: Source refuses automated access; do not keep retrying it.
    BLOCKED = "blocked"


FAILURE_STATUSES: frozenset[ScraperStatus] = frozenset(
    {ScraperStatus.FAILED, ScraperStatus.BLOCKED}
)


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class ScraperHealth:
    company: str
    provider: str
    status: ScraperStatus
    timestamp: datetime = field(default_factory=utcnow)
    attempts: int = 1
    duration_ms: int = 0
    jobs_found: int = 0
    jobs_malformed: int = 0
    relevant_jobs: int = 0
    new_jobs: int = 0
    updated_jobs: int = 0
    error_type: str | None = None
    error: str | None = None
    #: Carried forward from poll to poll: has this source *ever* fetched
    #: successfully? Only the latest row is stored, so this is what lets the first
    #: success of a new event source be recognised (and stored without alerts)
    #: even when earlier attempts failed.
    ever_succeeded: bool = False
    #: Failures in a row, carried forward; reset by a success. Event sources that
    #: keep failing are polled hourly instead of every poll (see PollRunner).
    consecutive_failures: int = 0

    @property
    def ok(self) -> bool:
        return self.status not in FAILURE_STATUSES

    def carried_from(self, previous: ScraperHealth | None) -> ScraperHealth:
        """This poll's row, with the history fields continued from ``previous``."""
        from dataclasses import replace

        before_ok = bool(previous and (previous.ever_succeeded or previous.ok))
        if self.ok:
            return replace(self, ever_succeeded=True, consecutive_failures=0)
        return replace(
            self,
            ever_succeeded=before_ok,
            consecutive_failures=(previous.consecutive_failures if previous else 0) + 1,
        )

    @property
    def key(self) -> str:
        return f"{self.company}::{self.provider}"

    def to_dict(self) -> dict[str, Any]:
        """Shape used both for the structured log line and for DynamoDB."""
        return {
            "company": self.company,
            "provider": self.provider,
            "status": self.status.value,
            "timestamp": self.timestamp.isoformat(),
            "attempts": self.attempts,
            "duration_ms": self.duration_ms,
            "jobs_found": self.jobs_found,
            "jobs_malformed": self.jobs_malformed,
            "relevant_jobs": self.relevant_jobs,
            "new_jobs": self.new_jobs,
            "updated_jobs": self.updated_jobs,
            "error_type": self.error_type,
            "error": self.error,
            "ever_succeeded": self.ever_succeeded,
            "consecutive_failures": self.consecutive_failures,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ScraperHealth:
        timestamp = data.get("timestamp")
        if isinstance(timestamp, str):
            parsed = datetime.fromisoformat(timestamp)
        elif isinstance(timestamp, datetime):
            parsed = timestamp
        else:
            parsed = utcnow()
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return cls(
            company=str(data["company"]),
            provider=str(data.get("provider", "unknown")),
            status=ScraperStatus(str(data.get("status", "failed"))),
            timestamp=parsed,
            attempts=int(data.get("attempts", 1) or 1),
            duration_ms=int(data.get("duration_ms", 0) or 0),
            jobs_found=int(data.get("jobs_found", 0) or 0),
            jobs_malformed=int(data.get("jobs_malformed", 0) or 0),
            relevant_jobs=int(data.get("relevant_jobs", 0) or 0),
            new_jobs=int(data.get("new_jobs", 0) or 0),
            updated_jobs=int(data.get("updated_jobs", 0) or 0),
            error_type=data.get("error_type") or None,
            error=data.get("error") or None,
            ever_succeeded=bool(data.get("ever_succeeded", False)),
            consecutive_failures=int(data.get("consecutive_failures", 0) or 0),
        )

    def with_counts(self, **counts: int) -> ScraperHealth:
        from dataclasses import replace

        return replace(self, **counts)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class PollSummary:
    """Run-level observability counters (PRD §25)."""

    scrapers_attempted: int = 0
    scrapers_succeeded: int = 0
    scrapers_failed: int = 0
    jobs_fetched: int = 0
    jobs_relevant: int = 0
    new_jobs: int = 0
    updated_jobs: int = 0
    notifications_emitted: int = 0
    notifications_failed: int = 0
    duration_ms: int = 0

    @classmethod
    def from_health(
        cls, records: Sequence[ScraperHealth], *, duration_ms: int = 0, **extra: int
    ) -> PollSummary:
        return cls(
            scrapers_attempted=len(records),
            scrapers_succeeded=sum(1 for r in records if r.ok),
            scrapers_failed=sum(1 for r in records if not r.ok),
            jobs_fetched=sum(r.jobs_found for r in records),
            jobs_relevant=sum(r.relevant_jobs for r in records),
            new_jobs=sum(r.new_jobs for r in records),
            updated_jobs=sum(r.updated_jobs for r in records),
            duration_ms=duration_ms,
            **extra,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "scrapers_attempted": self.scrapers_attempted,
            "scrapers_succeeded": self.scrapers_succeeded,
            "scrapers_failed": self.scrapers_failed,
            "jobs_fetched": self.jobs_fetched,
            "jobs_relevant": self.jobs_relevant,
            "new_jobs": self.new_jobs,
            "updated_jobs": self.updated_jobs,
            "notifications_emitted": self.notifications_emitted,
            "notifications_failed": self.notifications_failed,
            "duration_ms": self.duration_ms,
        }


__all__: Sequence[str] = (
    "FAILURE_STATUSES",
    "PollSummary",
    "ScraperHealth",
    "ScraperStatus",
    "utcnow",
)
