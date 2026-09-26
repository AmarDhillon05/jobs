"""Storage interfaces (PRD §10).

Two implementations behind these: :mod:`jobmonitor.storage.memory` (fast tests,
local dev, the local API server) and :mod:`jobmonitor.storage.dynamo` (the real
thing, exercised under Moto and LocalStack). Nothing above this layer knows which
it is talking to, which is what lets the same end-to-end acceptance scenarios run
in-process *and* against emulated AWS.

The contract that matters is :meth:`JobRepository.upsert`: it must be idempotent,
must keep ``first_seen`` stable forever, must move ``last_seen`` on every
sighting, and must report whether the posting was new - all of which the shared
test suite in ``tests/integration/test_repository_contract.py`` asserts against
both implementations.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from datetime import datetime

from jobmonitor.models.company import Company
from jobmonitor.models.health import ScraperHealth
from jobmonitor.models.job import Job
from jobmonitor.models.record import DeviceRegistration, JobRecord, UpsertResult


class JobRepository(ABC):
    """Persistence for discovered jobs."""

    @abstractmethod
    def upsert(
        self,
        job: Job,
        *,
        relevance_score: int = 0,
        company: Company | None = None,
        now: datetime | None = None,
    ) -> UpsertResult:
        """Record a sighting of ``job``.

        Idempotent: calling this twice with the same unchanged job must produce
        ``is_new=False`` the second time, leave ``first_seen`` untouched, and
        advance ``last_seen``.
        """

    @abstractmethod
    def get(self, job_id: str) -> JobRecord | None:
        """One record by its stable id, or ``None``."""

    @abstractmethod
    def recent(self, *, limit: int = 50, since: datetime | None = None) -> list[JobRecord]:
        """Most recently *discovered* jobs, newest ``first_seen`` first."""

    @abstractmethod
    def pending_notifications(self, *, limit: int = 100) -> list[JobRecord]:
        """Records that still need an alert sent."""

    @abstractmethod
    def mark_notified(self, job_ids: Iterable[str], *, now: datetime | None = None) -> int:
        """Flag records as alerted. Returns how many were changed.

        Must be safe to call twice: the second call changes nothing. This is the
        backstop against a duplicate notification when a queue redelivers.
        """

    @abstractmethod
    def by_company(self, company: str, *, limit: int = 50) -> list[JobRecord]:
        """Records for one company, newest first."""

    def count(self) -> int:
        """Total records. Overridden where the store can answer it cheaply."""
        return len(self.recent(limit=10_000))


class HealthRepository(ABC):
    """Persistence for per-company scraper health (PRD §16, §25)."""

    @abstractmethod
    def record(self, health: ScraperHealth) -> None:
        """Store the latest health for one company/provider pair."""

    def record_many(self, records: Iterable[ScraperHealth]) -> int:
        count = 0
        for health in records:
            self.record(health)
            count += 1
        return count

    @abstractmethod
    def latest(self) -> list[ScraperHealth]:
        """Latest health for every known company, newest first."""

    @abstractmethod
    def for_company(self, company: str) -> ScraperHealth | None: ...

    def failures(self) -> list[ScraperHealth]:
        return [health for health in self.latest() if not health.ok]


class DeviceRepository(ABC):
    """Persistence for push-notification targets (PRD §24)."""

    @abstractmethod
    def register(self, device: DeviceRegistration) -> DeviceRegistration: ...

    @abstractmethod
    def unregister(self, device_id: str) -> bool: ...

    @abstractmethod
    def enabled_devices(self) -> list[DeviceRegistration]: ...


__all__: Sequence[str] = ("DeviceRepository", "HealthRepository", "JobRepository")
