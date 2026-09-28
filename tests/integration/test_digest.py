"""Level 4 - the hourly email digest.

The rules pinned here:

* an hour with nothing new sends **no** email;
* each run covers exactly the previous complete window, so consecutive runs
  neither overlap nor leave gaps, even when a run starts late;
* every stored job appears - strong matches first, lower-relevance ones listed
  separately rather than dropped (the filter is deliberately loose);
* the Lambda handler takes the window from the schedule's own ``time``, so a
  retry covers the same hour;
* the same holds against DynamoDB (Moto), not just the in-memory store.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from tests.conftest import make_company

from jobmonitor.config import AwsSettings, EmailSettings, Settings, for_tests
from jobmonitor.models.job import Job
from jobmonitor.notifications.digest import DigestSender, digest_window, format_digest
from jobmonitor.notifications.transports import DeliveryError, MemoryEmailTransport
from jobmonitor.storage.base import JobRepository
from jobmonitor.storage.memory import InMemoryJobRepository

pytestmark = pytest.mark.integration

HOUR = datetime(2026, 9, 26, 13, 0, tzinfo=UTC)  # the window 13:00-14:00
RUN = HOUR + timedelta(hours=1, minutes=2)  # the 14:02 schedule that covers it
AWS = AwsSettings(region="us-east-1", jobs_table="digest-jobs", health_table="digest-health")


def settings(**email: object) -> Settings:
    return for_tests(
        aws=AWS,
        email=EmailSettings(transport="memory", recipient="me@example.test", **email),  # type: ignore[arg-type]
    )


def store(
    repository: JobRepository,
    identifier: str,
    *,
    at: datetime,
    company: str = "Stripe",
    title: str = "Software Engineer Intern",
    score: int = 90,
) -> None:
    job = Job(
        company=company,
        title=title,
        url=f"https://jobs.example.test/{company.lower().replace(' ', '-')}/{identifier}",
        source="greenhouse",
        external_id=identifier,
        location="New York",
    )
    repository.upsert(job, relevance_score=score, company=make_company(company), now=at)


def sender(repository: JobRepository, **email: object) -> tuple[DigestSender, MemoryEmailTransport]:
    transport = MemoryEmailTransport()
    return DigestSender(
        settings(**email), repository=repository, email_transport=transport
    ), transport


class TestWindow:
    @pytest.mark.parametrize(
        ("now", "start"),
        [
            (datetime(2026, 9, 26, 14, 2, tzinfo=UTC), datetime(2026, 9, 26, 13, 0, tzinfo=UTC)),
            (datetime(2026, 9, 26, 14, 0, tzinfo=UTC), datetime(2026, 9, 26, 13, 0, tzinfo=UTC)),
            (
                datetime(2026, 9, 26, 14, 59, 59, tzinfo=UTC),
                datetime(2026, 9, 26, 13, 0, tzinfo=UTC),
            ),
            (datetime(2026, 9, 27, 0, 5, tzinfo=UTC), datetime(2026, 9, 26, 23, 0, tzinfo=UTC)),
        ],
    )
    def test_covers_the_previous_complete_hour(self, now: datetime, start: datetime) -> None:
        assert digest_window(now, 60) == (start, start + timedelta(hours=1))

    def test_consecutive_runs_tile_time_even_when_one_starts_late(self) -> None:
        first = digest_window(datetime(2026, 9, 26, 14, 2, tzinfo=UTC), 60)
        late = digest_window(datetime(2026, 9, 26, 15, 40, tzinfo=UTC), 60)
        assert first[1] == late[0]

    def test_a_naive_time_is_treated_as_utc(self) -> None:
        assert digest_window(datetime(2026, 9, 26, 14, 2), 60)[0] == HOUR

    def test_other_window_lengths(self) -> None:
        assert digest_window(datetime(2026, 9, 26, 14, 20, tzinfo=UTC), 30) == (
            datetime(2026, 9, 26, 13, 30, tzinfo=UTC),
            datetime(2026, 9, 26, 14, 0, tzinfo=UTC),
        )


class TestSending:
    def test_an_empty_hour_sends_nothing(self) -> None:
        digest, email = sender(InMemoryJobRepository())
        outcome = digest.send(now=RUN)
        assert outcome.skipped and not outcome.sent
        assert email.sent == []
        assert outcome.to_dict()["skipped_empty"] is True

    def test_an_hour_with_jobs_sends_one_email_to_the_recipient(self) -> None:
        repository = InMemoryJobRepository()
        store(repository, "1", at=HOUR + timedelta(minutes=10))
        store(repository, "2", at=HOUR + timedelta(minutes=50), company="Jane Street")
        digest, email = sender(repository)

        outcome = digest.send(now=RUN)
        assert outcome.sent and len(outcome.jobs) == 2
        [(message, to, sender_address)] = email.sent
        assert to == "me@example.test"
        assert sender_address == "alerts@example.com"
        assert message.subject == "[Internships] 2 new internships - Jane Street, Stripe"

    def test_only_jobs_first_seen_inside_the_window_are_included(self) -> None:
        repository = InMemoryJobRepository()
        store(repository, "before", at=HOUR - timedelta(seconds=1))
        store(repository, "start", at=HOUR)
        store(repository, "end", at=HOUR + timedelta(hours=1))
        digest, _email = sender(repository)
        outcome = digest.send(now=RUN)
        assert [r.external_id for r in outcome.jobs] == ["start"]

    def test_a_job_seen_again_later_is_not_repeated_in_the_next_digest(self) -> None:
        repository = InMemoryJobRepository()
        store(repository, "1", at=HOUR + timedelta(minutes=5))
        store(repository, "1", at=HOUR + timedelta(minutes=65))  # the next poll saw it again
        digest, email = sender(repository)
        assert digest.send(now=RUN).sent
        assert digest.send(now=RUN + timedelta(hours=1)).skipped
        assert len(email.sent) == 1

    def test_a_delivery_failure_propagates_so_the_schedule_retries(self) -> None:
        repository = InMemoryJobRepository()
        store(repository, "1", at=HOUR + timedelta(minutes=5))
        digest, email = sender(repository)
        email.fail_on_call = 1
        with pytest.raises(DeliveryError):
            digest.send(now=RUN)
        # The retry (same event time) sends it.
        assert digest.send(now=RUN).sent

    def test_an_explicit_window_overrides_the_schedule(self) -> None:
        repository = InMemoryJobRepository()
        store(repository, "1", at=HOUR + timedelta(minutes=5))
        digest, _ = sender(repository)
        outcome = digest.send(window=(HOUR, HOUR + timedelta(minutes=6)))
        assert len(outcome.jobs) == 1


class TestFormat:
    def records(self) -> list:  # type: ignore[type-arg]
        repository = InMemoryJobRepository()
        store(repository, "a", at=HOUR, company="Zeta", score=60)
        store(repository, "b", at=HOUR, company="Alpha", score=95)
        store(repository, "c", at=HOUR, company="Beta", title="Data Analyst Intern", score=40)
        return repository.recent(limit=10)

    def test_strong_matches_come_first_and_the_rest_are_listed_not_dropped(self) -> None:
        message = format_digest(
            self.records(), window_start=HOUR, window_end=HOUR + timedelta(hours=1), strong=55
        )
        text = message.text_body
        strong, _, rest = text.partition("ALSO FOUND (lower relevance)")
        assert "STRONG MATCHES" in strong
        assert strong.index("Alpha") < strong.index("Zeta")  # by relevance
        assert "Beta: Data Analyst Intern" in rest
        assert "https://jobs.example.test/beta/c" in rest

    def test_the_text_names_the_window_and_every_link(self) -> None:
        message = format_digest(
            self.records(), window_start=HOUR, window_end=HOUR + timedelta(hours=1), strong=55
        )
        assert "2026-09-26 13:00 UTC - 2026-09-26 14:00 UTC" in message.text_body
        for company in ("alpha/b", "zeta/a", "beta/c"):
            assert f"https://jobs.example.test/{company}" in message.text_body
            assert f"https://jobs.example.test/{company}" in message.html_body

    def test_html_escapes_what_the_source_gave_us(self) -> None:
        repository = InMemoryJobRepository()
        store(repository, "x", at=HOUR, title="SWE Intern <script>alert(1)</script>")
        message = format_digest(repository.recent(), window_start=HOUR, window_end=HOUR, strong=55)
        assert "<script>" not in message.html_body
        assert "&lt;script&gt;" in message.html_body

    def test_the_subject_summarises_many_companies(self) -> None:
        repository = InMemoryJobRepository()
        for index, company in enumerate(["A", "B", "C", "D", "E"]):
            store(repository, str(index), at=HOUR, company=company)
        message = format_digest(repository.recent(), window_start=HOUR, window_end=HOUR, strong=55)
        assert message.subject == "[Internships] 5 new internships - A, B, C +2 more"

    def test_an_empty_digest_cannot_be_formatted(self) -> None:
        with pytest.raises(ValueError):
            format_digest([], window_start=HOUR, window_end=HOUR, strong=55)


class TestAgainstDynamoDB:
    @pytest.fixture
    def repository(self) -> Iterator[JobRepository]:
        import boto3
        from moto import mock_aws

        from jobmonitor.storage.dynamo import DynamoJobRepository
        from jobmonitor.storage.tables import create_tables

        with mock_aws():
            resource = boto3.resource("dynamodb", region_name="us-east-1")
            create_tables(resource, AWS, wait=False)
            yield DynamoJobRepository(AWS, resource=resource)

    def test_the_window_query_works_on_the_recent_index(self, repository: JobRepository) -> None:
        store(repository, "old", at=HOUR - timedelta(hours=3))
        store(repository, "new", at=HOUR + timedelta(minutes=30))
        digest, email = sender(repository)
        outcome = digest.send(now=RUN)
        assert [r.external_id for r in outcome.jobs] == ["new"]
        assert len(email.sent) == 1
        assert digest.send(now=RUN + timedelta(hours=1)).skipped


class TestHandler:
    def deps(self, repository: JobRepository, transport: MemoryEmailTransport):  # type: ignore[no-untyped-def]
        from jobmonitor.orchestration.handlers import Dependencies

        class Deps(Dependencies):
            def digest_sender(self) -> DigestSender:
                return DigestSender(self.settings, repository=repository, email_transport=transport)

        return Deps(
            settings=settings(),
            registry=None,  # type: ignore[arg-type]
            repository=repository,
            health=None,  # type: ignore[arg-type]
            devices=None,  # type: ignore[arg-type]
            scrape_queue=None,  # type: ignore[arg-type]
            notification_topic=None,  # type: ignore[arg-type]
        )

    def test_uses_the_schedule_time_not_the_clock(self) -> None:
        from jobmonitor.orchestration.handlers import digest_handler

        repository = InMemoryJobRepository()
        store(repository, "1", at=HOUR + timedelta(minutes=5))
        transport = MemoryEmailTransport()
        event = {"source": "aws.events", "time": "2026-09-26T14:02:00Z"}
        summary = digest_handler(event, None, deps=self.deps(repository, transport))
        assert summary["sent"] is True and summary["jobs"] == 1
        assert summary["window_start"] == "2026-09-26T13:00:00+00:00"
        assert len(transport.sent) == 1

    def test_an_empty_hour_returns_a_skip_summary(self) -> None:
        from jobmonitor.orchestration.handlers import digest_handler

        transport = MemoryEmailTransport()
        summary = digest_handler(
            {"time": "2026-09-26T14:02:00Z"},
            None,
            deps=self.deps(InMemoryJobRepository(), transport),
        )
        assert summary == {
            "window_start": "2026-09-26T13:00:00+00:00",
            "window_end": "2026-09-26T14:00:00+00:00",
            "jobs": 0,
            "sent": False,
            "skipped_empty": True,
        }
        assert transport.sent == []

    def test_an_unreadable_time_falls_back_to_now(self) -> None:
        from jobmonitor.orchestration.handlers import digest_handler

        summary = digest_handler(
            {"time": "not a time"},
            None,
            deps=self.deps(InMemoryJobRepository(), MemoryEmailTransport()),
        )
        end = datetime.fromisoformat(str(summary["window_end"]))
        assert end <= datetime.now(UTC) < end + timedelta(hours=1)
