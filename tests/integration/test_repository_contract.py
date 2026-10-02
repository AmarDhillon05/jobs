"""Level 3 - persistence, run against BOTH repository implementations.

Covers the PRD §14 Level-3 list exactly: insert new job, reinsert same job,
first_seen stability, last_seen update, notification flag, content update, recent
jobs query, idempotency - plus the dedup and notification-suppression properties
Gate B calls for.

Every test here runs twice: once in memory, once against real DynamoDB semantics
under Moto. A behaviour that only holds for one of them is a bug in that one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from tests.conftest import make_company

from jobmonitor.identity import job_id
from jobmonitor.models.health import ScraperHealth, ScraperStatus
from jobmonitor.models.job import Job
from jobmonitor.models.record import DeviceRegistration
from jobmonitor.storage.base import DeviceRepository, HealthRepository, JobRepository

pytestmark = pytest.mark.integration

T0 = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
T1 = T0 + timedelta(minutes=10)
T2 = T0 + timedelta(minutes=20)


def make_job(identifier: str = "1", **kwargs: object) -> Job:
    payload: dict[str, object] = {
        "company": "TestCo",
        "title": "Software Engineer Intern",
        "url": f"https://job-boards.greenhouse.io/testco/jobs/{identifier}",
        "source": "greenhouse",
        "external_id": identifier,
        "location": "San Francisco, CA",
    }
    payload.update(kwargs)
    return Job(**payload)  # type: ignore[arg-type]


class TestInsert:
    def test_a_new_job_is_reported_as_new(self, repository: JobRepository) -> None:
        result = repository.upsert(make_job(), relevance_score=73, now=T0)
        assert result.is_new is True
        assert result.is_updated is False
        assert result.should_notify is True

    def test_the_stored_record_carries_everything_the_prd_lists(
        self, repository: JobRepository
    ) -> None:
        job = make_job(description="Build systems", employment_type="Intern")
        repository.upsert(job, relevance_score=73, company=make_company(), now=T0)
        record = repository.get(job_id(job))
        assert record is not None
        assert record.job_id == job_id(job)
        assert record.company == "TestCo"
        assert record.title == "Software Engineer Intern"
        assert record.location == "San Francisco, CA"
        assert record.url == job.url
        assert record.external_id == "1"
        assert record.first_seen == T0
        assert record.last_seen == T0
        assert record.notification_sent is False
        assert record.source == "greenhouse"
        assert record.relevance_score == 73
        assert record.content_hash
        assert record.times_seen == 1
        assert record.priority == "high"
        assert record.industry == "Testing"

    def test_get_returns_none_for_an_unknown_id(self, repository: JobRepository) -> None:
        assert repository.get("nobody:0") is None

    def test_jobs_without_external_ids_are_still_stored_distinctly(
        self, repository: JobRepository
    ) -> None:
        a = make_job(external_id=None, url="https://x.example.com/a")
        b = make_job(external_id=None, url="https://x.example.com/b")
        assert repository.upsert(a, now=T0).is_new
        assert repository.upsert(b, now=T0).is_new
        assert repository.count() == 2


class TestIdempotency:
    def test_reinserting_the_same_job_is_not_new(self, repository: JobRepository) -> None:
        job = make_job()
        repository.upsert(job, now=T0)
        second = repository.upsert(job, now=T1)
        assert second.is_new is False
        assert second.is_updated is False
        assert second.should_notify is False

    def test_reinserting_does_not_duplicate_the_record(self, repository: JobRepository) -> None:
        job = make_job()
        for _ in range(5):
            repository.upsert(job, now=T1)
        assert repository.count() == 1

    def test_first_seen_never_moves(self, repository: JobRepository) -> None:
        # The authoritative detection timestamp (PRD §9).
        job = make_job()
        repository.upsert(job, now=T0)
        repository.upsert(job, now=T1)
        repository.upsert(job, now=T2)
        record = repository.get(job_id(job))
        assert record is not None
        assert record.first_seen == T0

    def test_last_seen_advances_on_every_sighting(self, repository: JobRepository) -> None:
        job = make_job()
        repository.upsert(job, now=T0)
        repository.upsert(job, now=T1)
        record = repository.get(job_id(job))
        assert record is not None
        assert record.last_seen == T1

    def test_times_seen_counts_sightings(self, repository: JobRepository) -> None:
        job = make_job()
        for _ in range(3):
            repository.upsert(job, now=T1)
        record = repository.get(job_id(job))
        assert record is not None
        assert record.times_seen == 3

    def test_tracking_parameters_do_not_create_a_second_record(
        self, repository: JobRepository
    ) -> None:
        repository.upsert(make_job(), now=T0)
        repository.upsert(
            make_job(url="https://job-boards.greenhouse.io/testco/jobs/1?utm_source=x"), now=T1
        )
        assert repository.count() == 1


class TestContentUpdates:
    def test_a_changed_title_is_reported_as_updated(self, repository: JobRepository) -> None:
        repository.upsert(make_job(), now=T0)
        result = repository.upsert(make_job(title="Backend Engineer Intern"), now=T1)
        assert result.is_new is False
        assert result.is_updated is True

    def test_an_update_refreshes_the_stored_content(self, repository: JobRepository) -> None:
        repository.upsert(make_job(), now=T0)
        repository.upsert(make_job(title="Backend Engineer Intern", location="Austin, TX"), now=T1)
        record = repository.get(job_id(make_job()))
        assert record is not None
        assert record.title == "Backend Engineer Intern"
        assert record.location == "Austin, TX"
        assert record.first_seen == T0

    def test_an_update_does_not_trigger_a_second_notification(
        self, repository: JobRepository
    ) -> None:
        # PRD §9: the same job must not alert twice, and a reworded description
        # is not a new job.
        repository.upsert(make_job(), now=T0)
        repository.mark_notified([job_id(make_job())], now=T0)
        result = repository.upsert(make_job(description="Reworded"), now=T1)
        assert result.is_updated is True
        assert result.should_notify is False

    def test_a_field_disappearing_from_the_source_clears_it(
        self, repository: JobRepository
    ) -> None:
        repository.upsert(make_job(location="Austin, TX"), now=T0)
        repository.upsert(make_job(location=None), now=T1)
        record = repository.get(job_id(make_job()))
        assert record is not None
        assert record.location is None

    def test_relevance_score_is_refreshed(self, repository: JobRepository) -> None:
        repository.upsert(make_job(), relevance_score=40, now=T0)
        repository.upsert(make_job(), relevance_score=73, now=T1)
        record = repository.get(job_id(make_job()))
        assert record is not None
        assert record.relevance_score == 73


class TestNotificationState:
    def test_a_new_record_starts_pending(self, repository: JobRepository) -> None:
        repository.upsert(make_job(), now=T0)
        pending = repository.pending_notifications()
        assert [record.job_id for record in pending] == [job_id(make_job())]

    def test_marking_notified_sets_the_flag_and_timestamp(self, repository: JobRepository) -> None:
        repository.upsert(make_job(), now=T0)
        assert repository.mark_notified([job_id(make_job())], now=T1) == 1
        record = repository.get(job_id(make_job()))
        assert record is not None
        assert record.notification_sent is True
        assert record.notified_at == T1

    def test_a_notified_record_leaves_the_pending_set(self, repository: JobRepository) -> None:
        repository.upsert(make_job(), now=T0)
        repository.mark_notified([job_id(make_job())], now=T1)
        assert repository.pending_notifications() == []

    def test_marking_notified_twice_changes_nothing(self, repository: JobRepository) -> None:
        # The backstop against a redelivered queue message alerting twice.
        repository.upsert(make_job(), now=T0)
        assert repository.mark_notified([job_id(make_job())], now=T1) == 1
        assert repository.mark_notified([job_id(make_job())], now=T2) == 0
        record = repository.get(job_id(make_job()))
        assert record is not None
        assert record.notified_at == T1

    def test_seeing_a_notified_job_again_does_not_make_it_pending(
        self, repository: JobRepository
    ) -> None:
        """The exact bug a sparse index invites: re-adding the pending marker."""
        repository.upsert(make_job(), now=T0)
        repository.mark_notified([job_id(make_job())], now=T0)
        for offset in range(1, 6):
            repository.upsert(make_job(), now=T0 + timedelta(minutes=10 * offset))
        assert repository.pending_notifications() == []
        record = repository.get(job_id(make_job()))
        assert record is not None
        assert record.notification_sent is True

    def test_baseline_marking_is_stored_and_leaves_the_pending_set(
        self, repository: JobRepository
    ) -> None:
        # The quiet first poll of a new event source.
        repository.upsert(make_job("1"), now=T0)
        repository.upsert(make_job("2"), now=T0)
        assert repository.mark_notified([job_id(make_job("1"))], now=T0, baseline=True) == 1
        quiet = repository.get(job_id(make_job("1")))
        normal = repository.get(job_id(make_job("2")))
        assert quiet is not None and quiet.notification_sent and quiet.baseline
        assert normal is not None and not normal.baseline
        assert [r.external_id for r in repository.pending_notifications()] == ["2"]
        repository.upsert(make_job("1"), now=T1)
        again = repository.get(job_id(make_job("1")))
        assert again is not None and again.baseline  # survives later sightings

    def test_marking_an_unknown_id_is_harmless(self, repository: JobRepository) -> None:
        assert repository.mark_notified(["ghost:0"], now=T1) == 0

    def test_pending_returns_oldest_first(self, repository: JobRepository) -> None:
        repository.upsert(make_job("1"), now=T0)
        repository.upsert(make_job("2"), now=T1)
        assert [r.external_id for r in repository.pending_notifications()] == ["1", "2"]

    def test_pending_respects_its_limit(self, repository: JobRepository) -> None:
        for index in range(5):
            repository.upsert(make_job(str(index)), now=T0 + timedelta(seconds=index))
        assert len(repository.pending_notifications(limit=2)) == 2


class TestRecentQuery:
    def _seed(self, repository: JobRepository, count: int = 5) -> None:
        for index in range(count):
            repository.upsert(
                make_job(str(index), title=f"Software Engineer Intern {index}"),
                now=T0 + timedelta(minutes=index),
            )

    def test_returns_newest_first(self, repository: JobRepository) -> None:
        self._seed(repository)
        recent = repository.recent(limit=10)
        assert [record.external_id for record in recent] == ["4", "3", "2", "1", "0"]

    def test_respects_the_limit(self, repository: JobRepository) -> None:
        self._seed(repository)
        assert len(repository.recent(limit=2)) == 2

    def test_since_filters_by_first_seen(self, repository: JobRepository) -> None:
        self._seed(repository)
        recent = repository.recent(limit=10, since=T0 + timedelta(minutes=3))
        assert [record.external_id for record in recent] == ["4", "3"]

    def test_empty_store_returns_empty(self, repository: JobRepository) -> None:
        assert repository.recent() == []

    def test_recent_reflects_first_seen_not_last_seen(self, repository: JobRepository) -> None:
        # An old job seen again must not jump to the top of the feed.
        self._seed(repository, count=3)
        repository.upsert(make_job("0"), now=T0 + timedelta(hours=5))
        assert repository.recent(limit=1)[0].external_id == "2"


class TestByCompany:
    def test_filters_to_one_company(self, repository: JobRepository) -> None:
        repository.upsert(make_job("1"), now=T0)
        repository.upsert(make_job("2", company="OtherCo"), now=T1)
        records = repository.by_company("TestCo")
        assert [record.external_id for record in records] == ["1"]

    def test_unknown_company_returns_empty(self, repository: JobRepository) -> None:
        assert repository.by_company("Nobody") == []


class TestRoundTrip:
    def test_a_record_survives_serialisation_unchanged(self, repository: JobRepository) -> None:
        job = make_job(
            description="Build things",
            employment_type="Intern",
            date_posted="2026-09-18T00:00:00Z",
        )
        repository.upsert(job, relevance_score=73, company=make_company(), now=T0)
        record = repository.get(job_id(job))
        assert record is not None
        assert record.date_posted == datetime(2026, 9, 18, tzinfo=UTC)
        assert record.description == "Build things"
        assert record.employment_type == "Intern"

    def test_api_dict_exposes_no_internal_bookkeeping(self, repository: JobRepository) -> None:
        repository.upsert(make_job(), now=T0)
        record = repository.get(job_id(make_job()))
        assert record is not None
        payload = record.to_api_dict()
        assert "notification_pending" not in payload
        assert "content_hash" not in payload
        assert "times_seen" not in payload
        assert payload["url"] == make_job().url


class TestHealthRepository:
    def health(self, company: str = "TestCo", **kwargs: object) -> ScraperHealth:
        payload: dict[str, object] = {
            "company": company,
            "provider": "greenhouse",
            "status": ScraperStatus.SUCCESS,
            "timestamp": T0,
            "jobs_found": 4,
        }
        payload.update(kwargs)
        return ScraperHealth(**payload)  # type: ignore[arg-type]

    def test_records_and_reads_back(self, health_repository: HealthRepository) -> None:
        health_repository.record(self.health())
        stored = health_repository.for_company("TestCo")
        assert stored is not None
        assert stored.status is ScraperStatus.SUCCESS
        assert stored.jobs_found == 4

    def test_latest_overwrites_the_previous_run(self, health_repository: HealthRepository) -> None:
        health_repository.record(self.health(status=ScraperStatus.SUCCESS))
        health_repository.record(
            self.health(status=ScraperStatus.FAILED, timestamp=T1, error="HTTP 403")
        )
        stored = health_repository.for_company("TestCo")
        assert stored is not None
        assert stored.status is ScraperStatus.FAILED
        assert stored.error == "HTTP 403"

    def test_failures_lists_only_broken_scrapers(self, health_repository: HealthRepository) -> None:
        health_repository.record_many(
            [
                self.health("GoodCo"),
                self.health("EmptyCo", status=ScraperStatus.EMPTY, jobs_found=0),
                self.health("BadCo", status=ScraperStatus.FAILED, error="boom"),
                self.health("BlockedCo", status=ScraperStatus.BLOCKED, error="403"),
            ]
        )
        assert {health.company for health in health_repository.failures()} == {
            "BadCo",
            "BlockedCo",
        }

    def test_error_details_from_the_prd_example_round_trip(
        self, health_repository: HealthRepository
    ) -> None:
        # PRD §16's exact record shape.
        health_repository.record(
            self.health(
                "Company A",
                provider="custom",
                status=ScraperStatus.FAILED,
                attempts=3,
                error_type="HTTPError",
                error="403",
            )
        )
        stored = health_repository.for_company("Company A")
        assert stored is not None
        assert (stored.provider, stored.attempts, stored.error_type, stored.error) == (
            "custom",
            3,
            "HTTPError",
            "403",
        )

    def test_get_by_key_and_history_fields_round_trip(
        self, health_repository: HealthRepository
    ) -> None:
        health_repository.record(self.health(provider="greenhouse"))
        health_repository.record(
            self.health(
                provider="event_page:campus",
                status=ScraperStatus.FAILED,
                ever_succeeded=True,
                consecutive_failures=4,
            )
        )
        stored = health_repository.get("TestCo::event_page:campus")
        assert stored is not None
        assert (stored.ever_succeeded, stored.consecutive_failures) == (True, 4)
        board = health_repository.get("TestCo::greenhouse")
        assert board is not None and board.ok
        assert health_repository.get("TestCo::luma") is None

    def test_unknown_company_returns_none(self, health_repository: HealthRepository) -> None:
        assert health_repository.for_company("Nobody") is None

    def test_latest_is_newest_first(self, health_repository: HealthRepository) -> None:
        health_repository.record(self.health("Old", timestamp=T0))
        health_repository.record(self.health("New", timestamp=T2))
        assert next(h.company for h in health_repository.latest()) == "New"


class TestDeviceRepository:
    def device(self, device_id: str = "device-1", **kwargs: object) -> DeviceRegistration:
        payload: dict[str, object] = {
            "device_id": device_id,
            "token": "fake-subscription-token",
            "transport": "webpush",
            "registered_at": T0,
            "last_seen": T0,
        }
        payload.update(kwargs)
        return DeviceRegistration(**payload)  # type: ignore[arg-type]

    def test_register_and_list(self, device_repository: DeviceRepository) -> None:
        device_repository.register(self.device())
        assert [d.device_id for d in device_repository.enabled_devices()] == ["device-1"]

    def test_reregistration_keeps_the_original_registration_time(
        self, device_repository: DeviceRepository
    ) -> None:
        device_repository.register(self.device())
        refreshed = device_repository.register(
            self.device(token="rotated-token", registered_at=T2, last_seen=T2)
        )
        assert refreshed.registered_at == T0
        assert refreshed.token == "rotated-token"
        assert len(device_repository.enabled_devices()) == 1

    def test_disabled_devices_are_excluded(self, device_repository: DeviceRepository) -> None:
        device_repository.register(self.device("on"))
        device_repository.register(self.device("off", enabled=False))
        assert [d.device_id for d in device_repository.enabled_devices()] == ["on"]

    def test_unregister(self, device_repository: DeviceRepository) -> None:
        device_repository.register(self.device())
        assert device_repository.unregister("device-1") is True
        assert device_repository.enabled_devices() == []

    def test_unregistering_an_unknown_device_is_false(
        self, device_repository: DeviceRepository
    ) -> None:
        assert device_repository.unregister("ghost") is False
