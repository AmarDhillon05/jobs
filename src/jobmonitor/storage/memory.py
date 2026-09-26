"""In-memory repositories.

Not only a test double: this is what ``make serve-api`` and ``make local`` use to
run the whole system with no AWS at all, and what the acceptance scenarios run
against before they are repeated on emulated AWS. Because it implements the same
contract (and is verified by the same shared contract test suite), a scenario
passing here and failing on DynamoDB isolates the problem to the AWS layer.

Thread-safe via a single lock, because the local API server serves requests on
threads while a poll may be writing.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Sequence
from datetime import datetime

from jobmonitor.identity import job_id as compute_job_id
from jobmonitor.models.company import Company
from jobmonitor.models.health import ScraperHealth
from jobmonitor.models.job import Job
from jobmonitor.models.record import DeviceRegistration, JobRecord, UpsertResult, utcnow
from jobmonitor.storage.base import DeviceRepository, HealthRepository, JobRepository


class InMemoryJobRepository(JobRepository):
    def __init__(self) -> None:
        self._records: dict[str, JobRecord] = {}
        self._lock = threading.RLock()

    def upsert(
        self,
        job: Job,
        *,
        relevance_score: int = 0,
        company: Company | None = None,
        now: datetime | None = None,
    ) -> UpsertResult:
        timestamp = now or utcnow()
        identity = compute_job_id(job)
        with self._lock:
            existing = self._records.get(identity)
            if existing is None:
                record = JobRecord.from_job(
                    job, relevance_score=relevance_score, company=company, now=timestamp
                )
                self._records[identity] = record
                return UpsertResult(record=record, is_new=True, is_updated=False)

            updated = existing.seen_again(job, now=timestamp, relevance_score=relevance_score)
            self._records[identity] = updated
            return UpsertResult(
                record=updated,
                is_new=False,
                is_updated=updated.content_hash != existing.content_hash,
            )

    def get(self, job_id: str) -> JobRecord | None:
        with self._lock:
            return self._records.get(job_id)

    def recent(self, *, limit: int = 50, since: datetime | None = None) -> list[JobRecord]:
        with self._lock:
            records = list(self._records.values())
        if since is not None:
            records = [record for record in records if record.first_seen >= since]
        records.sort(key=lambda record: (record.first_seen, record.job_id), reverse=True)
        return records[: max(0, limit)]

    def pending_notifications(self, *, limit: int = 100) -> list[JobRecord]:
        with self._lock:
            pending = [r for r in self._records.values() if r.is_pending_notification]
        pending.sort(key=lambda record: (record.first_seen, record.job_id))
        return pending[: max(0, limit)]

    def mark_notified(self, job_ids: Iterable[str], *, now: datetime | None = None) -> int:
        timestamp = now or utcnow()
        changed = 0
        with self._lock:
            for job_id in job_ids:
                record = self._records.get(job_id)
                # Already-notified records are left alone: that is what makes a
                # redelivered queue message harmless.
                if record is not None and not record.notification_sent:
                    self._records[job_id] = record.marked_notified(timestamp)
                    changed += 1
        return changed

    def by_company(self, company: str, *, limit: int = 50) -> list[JobRecord]:
        folded = company.casefold()
        with self._lock:
            matches = [r for r in self._records.values() if r.company.casefold() == folded]
        matches.sort(key=lambda record: (record.first_seen, record.job_id), reverse=True)
        return matches[: max(0, limit)]

    def count(self) -> int:
        with self._lock:
            return len(self._records)

    # ------------------------------------------------------------- test helpers
    def clear(self) -> None:
        with self._lock:
            self._records.clear()

    def all_records(self) -> list[JobRecord]:
        with self._lock:
            return list(self._records.values())


class InMemoryHealthRepository(HealthRepository):
    def __init__(self) -> None:
        self._records: dict[str, ScraperHealth] = {}
        self._lock = threading.RLock()

    def record(self, health: ScraperHealth) -> None:
        with self._lock:
            self._records[health.key] = health

    def latest(self) -> list[ScraperHealth]:
        with self._lock:
            records = list(self._records.values())
        records.sort(key=lambda health: (health.timestamp, health.company), reverse=True)
        return records

    def for_company(self, company: str) -> ScraperHealth | None:
        folded = company.casefold()
        for health in self.latest():
            if health.company.casefold() == folded:
                return health
        return None

    def clear(self) -> None:
        with self._lock:
            self._records.clear()


class InMemoryDeviceRepository(DeviceRepository):
    def __init__(self) -> None:
        self._devices: dict[str, DeviceRegistration] = {}
        self._lock = threading.RLock()

    def register(self, device: DeviceRegistration) -> DeviceRegistration:
        with self._lock:
            existing = self._devices.get(device.device_id)
            # Re-registration keeps the original registered_at: it is the same
            # device refreshing its token, not a new one.
            if existing is not None:
                from dataclasses import replace

                device = replace(device, registered_at=existing.registered_at)
            self._devices[device.device_id] = device
            return device

    def unregister(self, device_id: str) -> bool:
        with self._lock:
            return self._devices.pop(device_id, None) is not None

    def enabled_devices(self) -> list[DeviceRegistration]:
        with self._lock:
            return [device for device in self._devices.values() if device.enabled]

    def clear(self) -> None:
        with self._lock:
            self._devices.clear()


__all__: Sequence[str] = (
    "InMemoryDeviceRepository",
    "InMemoryHealthRepository",
    "InMemoryJobRepository",
)
