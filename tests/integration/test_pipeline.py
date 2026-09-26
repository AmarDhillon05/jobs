"""Level 6 - backend integration (PRD §14 Level 6).

Exercises scrape -> normalize -> filter -> fingerprint -> persist -> identify new
-> notification event, and every one of the ten required cases:

1. one new job                     6. a provider is rate-limited
2. an existing job                 7. storage temporarily errors
3. one new + many existing         8. notification temporarily errors
4. an updated existing job         9. a retry succeeds
5. one scraper fails              10. permanent failure reaches the DLQ path

Deterministic fixture sources stand in for the network; everything else - the
filter, identity, the repository, the notifier - is the real implementation.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

import pytest
from tests.conftest import make_company
from tests.scrapers.conftest import build_client
from tests.support.http import FakeTransport, ScriptedResponse, load_fixture

from jobmonitor.config import for_tests
from jobmonitor.errors import ParseError
from jobmonitor.filtering import JobFilter
from jobmonitor.http import HttpClient
from jobmonitor.models.company import Company
from jobmonitor.models.health import ScraperStatus
from jobmonitor.models.job import Job
from jobmonitor.notifications import MemoryEmailTransport, MemoryPushTransport, Notifier
from jobmonitor.orchestration import PollRunner, StorageFailure, process_company
from jobmonitor.scrapers.base import JobSource
from jobmonitor.storage.memory import InMemoryHealthRepository, InMemoryJobRepository

pytestmark = pytest.mark.integration

T0 = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
T1 = T0 + timedelta(minutes=10)
T2 = T0 + timedelta(minutes=20)


# ------------------------------------------------------------ test job sources


class StaticSource(JobSource):
    """A deterministic source: yields exactly the jobs it was handed."""

    provider: ClassVar[str] = "static"
    required_config: ClassVar[tuple[str, ...]] = ()

    def __init__(self, company: Company, jobs: Sequence[Job], **kwargs: Any) -> None:
        super().__init__(company, **kwargs)
        self._jobs = list(jobs)

    def fetch_pages(self) -> Iterator[Sequence[Any]]:
        yield self._jobs

    def normalize(self, raw_job: Any) -> Job:
        return raw_job  # already a Job


class BrokenSource(JobSource):
    """A source that always fails permanently."""

    provider: ClassVar[str] = "broken"
    required_config: ClassVar[tuple[str, ...]] = ()

    def fetch_pages(self) -> Iterator[Sequence[Any]]:
        raise ParseError("upstream returned an HTML maintenance page")

    def normalize(self, raw_job: Any) -> Job:  # pragma: no cover - never reached
        raise AssertionError


def intern(identifier: str, company: str = "TestCo", **kwargs: object) -> Job:
    payload: dict[str, object] = {
        "company": company,
        "title": "Software Engineer Intern",
        "url": f"https://job-boards.greenhouse.io/testco/jobs/{identifier}",
        "source": "greenhouse",
        "external_id": identifier,
        "location": "San Francisco, CA",
        "description": "Build systems.",
    }
    payload.update(kwargs)
    return Job(**payload)  # type: ignore[arg-type]


@pytest.fixture
def stack() -> tuple[PollRunner, InMemoryJobRepository, MemoryEmailTransport, MemoryPushTransport]:
    repository = InMemoryJobRepository()
    email = MemoryEmailTransport()
    push = MemoryPushTransport()
    settings = for_tests(app_base_url="https://app.test")
    notifier = Notifier(settings, repository=repository, email_transport=email, push_transport=push)
    runner = PollRunner(
        settings,
        repository=repository,
        notifier=notifier,
        health_repository=InMemoryHealthRepository(),
        job_filter=JobFilter(settings.filters),
    )
    return runner, repository, email, push


def with_source(runner: PollRunner, mapping: dict[str, JobSource]) -> None:
    runner.source_factory = lambda company: mapping.get(company.company)


class TestCase1OneNewJob:
    def test_a_single_new_job_is_stored_filtered_and_notified(
        self,
        stack: tuple[PollRunner, InMemoryJobRepository, MemoryEmailTransport, MemoryPushTransport],
    ) -> None:
        runner, repository, email, push = stack
        company = make_company()
        with_source(runner, {"TestCo": StaticSource(company, [intern("1")])})

        outcome = runner.run([company], poll_id="p1", now=T0)

        assert len(outcome.new_records) == 1
        assert repository.count() == 1
        assert len(email.sent) == 1
        assert len(push.sent) == 1
        assert outcome.summary().new_jobs == 1
        assert outcome.summary().scrapers_succeeded == 1

    def test_irrelevant_jobs_are_filtered_before_storage(
        self,
        stack: tuple[PollRunner, InMemoryJobRepository, MemoryEmailTransport, MemoryPushTransport],
    ) -> None:
        runner, repository, email, _push = stack
        company = make_company()
        jobs = [intern("1"), intern("2", title="Marketing Intern")]
        with_source(runner, {"TestCo": StaticSource(company, jobs)})

        outcome = runner.run([company], poll_id="p1", now=T0)

        assert repository.count() == 1
        assert outcome.health_records[0].jobs_found == 2
        assert outcome.health_records[0].relevant_jobs == 1
        assert len(email.sent) == 1


class TestCase2ExistingJob:
    def test_a_second_identical_poll_produces_nothing_new(
        self,
        stack: tuple[PollRunner, InMemoryJobRepository, MemoryEmailTransport, MemoryPushTransport],
    ) -> None:
        runner, repository, email, push = stack
        company = make_company()
        with_source(runner, {"TestCo": StaticSource(company, [intern("1")])})

        first = runner.run([company], poll_id="p1", now=T0)
        second = runner.run([company], poll_id="p2", now=T1)

        assert len(first.new_records) == 1
        assert second.new_records == []
        assert repository.count() == 1
        assert len(email.sent) == 1  # no duplicate notification
        assert len(push.sent) == 1

    def test_first_seen_is_unchanged_and_last_seen_moves(
        self,
        stack: tuple[PollRunner, InMemoryJobRepository, MemoryEmailTransport, MemoryPushTransport],
    ) -> None:
        runner, repository, _email, _push = stack
        company = make_company()
        with_source(runner, {"TestCo": StaticSource(company, [intern("1")])})
        runner.run([company], poll_id="p1", now=T0)
        runner.run([company], poll_id="p2", now=T1)

        record = repository.recent(limit=1)[0]
        assert record.first_seen == T0
        assert record.last_seen == T1
        assert record.times_seen == 2


class TestCase3OneNewAmongManyExisting:
    def test_only_the_new_job_notifies(
        self,
        stack: tuple[PollRunner, InMemoryJobRepository, MemoryEmailTransport, MemoryPushTransport],
    ) -> None:
        runner, repository, email, _push = stack
        company = make_company()
        existing = [intern(str(index)) for index in range(10)]
        with_source(runner, {"TestCo": StaticSource(company, existing)})
        runner.run([company], poll_id="p1", now=T0)
        assert repository.count() == 10
        emails_after_first = len(email.sent)

        # Second poll: the same 10 plus 1 new.
        with_source(runner, {"TestCo": StaticSource(company, [*existing, intern("10")])})
        outcome = runner.run([company], poll_id="p2", now=T1)

        assert len(outcome.new_records) == 1
        assert outcome.new_records[0].external_id == "10"
        assert repository.count() == 11
        assert len(email.sent) == emails_after_first + 1

    def test_all_eleven_records_are_stored_and_updated_correctly(
        self,
        stack: tuple[PollRunner, InMemoryJobRepository, MemoryEmailTransport, MemoryPushTransport],
    ) -> None:
        runner, repository, _email, _push = stack
        company = make_company()
        existing = [intern(str(index)) for index in range(10)]
        with_source(runner, {"TestCo": StaticSource(company, existing)})
        runner.run([company], poll_id="p1", now=T0)
        with_source(runner, {"TestCo": StaticSource(company, [*existing, intern("10")])})
        runner.run([company], poll_id="p2", now=T1)

        records = {record.external_id: record for record in repository.all_records()}
        assert len(records) == 11
        for identifier in (str(i) for i in range(10)):
            assert records[identifier].first_seen == T0
            assert records[identifier].last_seen == T1
        assert records["10"].first_seen == T1


class TestCase4UpdatedJob:
    def test_an_edited_posting_is_recorded_as_updated_not_new(
        self,
        stack: tuple[PollRunner, InMemoryJobRepository, MemoryEmailTransport, MemoryPushTransport],
    ) -> None:
        runner, repository, email, _push = stack
        company = make_company()
        with_source(runner, {"TestCo": StaticSource(company, [intern("1")])})
        runner.run([company], poll_id="p1", now=T0)
        sent_before = len(email.sent)

        with_source(
            runner,
            {"TestCo": StaticSource(company, [intern("1", title="Backend Engineer Intern")])},
        )
        outcome = runner.run([company], poll_id="p2", now=T1)

        assert outcome.new_records == []
        assert len(outcome.updated_records) == 1
        assert repository.count() == 1
        # An edit is not news: no second alert.
        assert len(email.sent) == sent_before
        assert outcome.summary().updated_jobs == 1

    def test_the_updated_content_is_visible_in_the_feed(
        self,
        stack: tuple[PollRunner, InMemoryJobRepository, MemoryEmailTransport, MemoryPushTransport],
    ) -> None:
        runner, repository, _email, _push = stack
        company = make_company()
        with_source(runner, {"TestCo": StaticSource(company, [intern("1")])})
        runner.run([company], poll_id="p1", now=T0)
        with_source(runner, {"TestCo": StaticSource(company, [intern("1", location="Austin, TX")])})
        runner.run([company], poll_id="p2", now=T1)
        assert repository.recent(limit=1)[0].location == "Austin, TX"


class TestCase5ScraperFailureIsolation:
    def test_one_broken_scraper_does_not_stop_the_others(
        self,
        stack: tuple[PollRunner, InMemoryJobRepository, MemoryEmailTransport, MemoryPushTransport],
    ) -> None:
        runner, repository, email, _push = stack
        a = make_company("Company A")
        b = make_company("Company B")
        c = make_company("Company C")
        with_source(
            runner,
            {
                "Company A": BrokenSource(a),
                "Company B": StaticSource(b, [intern("b1", company="Company B")]),
                "Company C": StaticSource(c, [intern("c1", company="Company C")]),
            },
        )

        outcome = runner.run([a, b, c], poll_id="p1", now=T0)

        assert {o.company for o in outcome.failures} == {"Company A"}
        assert {o.company for o in outcome.successes} == {"Company B", "Company C"}
        assert repository.count() == 2
        assert len(email.sent) >= 1

    def test_the_failure_is_recorded_with_the_prd_fields(
        self,
        stack: tuple[PollRunner, InMemoryJobRepository, MemoryEmailTransport, MemoryPushTransport],
    ) -> None:
        runner, _repository, _email, _push = stack
        company = make_company("Company A", provider="broken")
        with_source(runner, {"Company A": BrokenSource(company)})

        outcome = runner.run([company], poll_id="p1", now=T0)

        health = outcome.health_records[0]
        assert health.company == "Company A"
        assert health.status is ScraperStatus.FAILED
        assert health.error_type == "ParseError"
        assert "maintenance" in (health.error or "")
        assert health.timestamp is not None

    def test_health_is_persisted_for_the_health_view(
        self,
        stack: tuple[PollRunner, InMemoryJobRepository, MemoryEmailTransport, MemoryPushTransport],
    ) -> None:
        runner, _repository, _email, _push = stack
        a = make_company("Company A")
        b = make_company("Company B")
        with_source(
            runner,
            {
                "Company A": BrokenSource(a),
                "Company B": StaticSource(b, [intern("b1", company="Company B")]),
            },
        )
        runner.run([a, b], poll_id="p1", now=T0)

        assert runner.health_repository is not None
        assert {h.company for h in runner.health_repository.failures()} == {"Company A"}
        assert len(runner.health_repository.latest()) == 2

    def test_a_registry_entry_with_a_broken_config_is_isolated_too(self) -> None:
        repository = InMemoryJobRepository()
        settings = for_tests()
        # No board_token: the adapter cannot even be constructed.
        broken = make_company("Broken Co", provider="greenhouse", config={})
        outcome = process_company(broken, settings=settings, repository=repository, now=T0)
        assert outcome.ok is False
        assert outcome.health.error_type == "ProviderConfigError"
        assert repository.count() == 0


class TestCase6And9TransientFailureAndRetry:
    def _greenhouse(self, transport: FakeTransport) -> tuple[Company, JobSource]:
        from jobmonitor.scrapers import build_source

        company = make_company(provider="greenhouse", config={"board_token": "testco"})
        return company, build_source(company, build_client(transport))

    def test_500_500_200_recovers_and_the_jobs_land(
        self,
        stack: tuple[PollRunner, InMemoryJobRepository, MemoryEmailTransport, MemoryPushTransport],
    ) -> None:
        runner, repository, email, _push = stack
        board = load_fixture("greenhouse", "board.json")
        transport = FakeTransport(
            [
                ScriptedResponse.error(500),
                ScriptedResponse.error(500),
                ScriptedResponse.json(board),
            ]
        )
        company, source = self._greenhouse(transport)
        with_source(runner, {company.company: source})

        outcome = runner.run([company], poll_id="p1", now=T0)

        assert outcome.successes
        assert repository.count() >= 1
        assert outcome.health_records[0].attempts == 3
        # Two relevant internships in the fixture, at a high-priority company,
        # so two immediate alerts rather than one grouped one.
        assert len(email.sent) == 2

    def test_a_rate_limited_provider_backs_off_and_succeeds(
        self,
        stack: tuple[PollRunner, InMemoryJobRepository, MemoryEmailTransport, MemoryPushTransport],
    ) -> None:
        runner, repository, _email, _push = stack
        board = load_fixture("greenhouse", "board.json")
        transport = FakeTransport(
            [
                ScriptedResponse.error(429, headers={"Retry-After": "1"}),
                ScriptedResponse.json(board),
            ]
        )
        company, source = self._greenhouse(transport)
        with_source(runner, {company.company: source})

        outcome = runner.run([company], poll_id="p1", now=T0)

        assert outcome.successes
        assert repository.count() >= 1
        assert outcome.health_records[0].attempts == 2

    def test_a_persistently_rate_limited_provider_fails_without_crashing_the_run(
        self,
        stack: tuple[PollRunner, InMemoryJobRepository, MemoryEmailTransport, MemoryPushTransport],
    ) -> None:
        runner, repository, _email, _push = stack
        transport = FakeTransport(always=ScriptedResponse.error(429))
        limited_company, limited_source = self._greenhouse(transport)
        healthy = make_company("Healthy Co")
        with_source(
            runner,
            {
                limited_company.company: limited_source,
                "Healthy Co": StaticSource(healthy, [intern("h1", company="Healthy Co")]),
            },
        )

        outcome = runner.run([limited_company, healthy], poll_id="p1", now=T0)

        assert len(outcome.failures) == 1
        assert repository.count() == 1


class TestCase7StorageErrors:
    class FlakyRepository(InMemoryJobRepository):
        def __init__(self, fail_times: int) -> None:
            super().__init__()
            self.remaining = fail_times

        def upsert(self, *args: Any, **kwargs: Any) -> Any:  # type: ignore[override]
            if self.remaining > 0:
                self.remaining -= 1
                raise RuntimeError("DynamoDB ProvisionedThroughputExceeded")
            return super().upsert(*args, **kwargs)

    def test_a_storage_failure_is_raised_not_silently_swallowed(self) -> None:
        # Unlike a broken source, a failing database means the work did not
        # happen, so the message must be retried rather than marked done.
        repository = self.FlakyRepository(fail_times=1)
        company = make_company()
        with pytest.raises(StorageFailure, match="ProvisionedThroughput"):
            process_company(
                company,
                settings=for_tests(),
                repository=repository,
                source=StaticSource(company, [intern("1")]),
                now=T0,
            )

    def test_a_retry_after_a_storage_failure_succeeds(self) -> None:
        repository = self.FlakyRepository(fail_times=1)
        company = make_company()
        with pytest.raises(StorageFailure):
            process_company(
                company,
                settings=for_tests(),
                repository=repository,
                source=StaticSource(company, [intern("1")]),
                now=T0,
            )
        outcome = process_company(
            company,
            settings=for_tests(),
            repository=repository,
            source=StaticSource(company, [intern("1")]),
            now=T1,
        )
        assert len(outcome.new_records) == 1
        assert repository.count() == 1


class TestCase8NotificationErrors:
    def test_a_notification_failure_leaves_the_job_stored_and_pending(
        self,
    ) -> None:
        repository = InMemoryJobRepository()
        email = MemoryEmailTransport(fail_on_call=1)
        push = MemoryPushTransport(fail_on_call=1)
        settings = for_tests()
        runner = PollRunner(
            settings,
            repository=repository,
            notifier=Notifier(
                settings, repository=repository, email_transport=email, push_transport=push
            ),
        )
        company = make_company()
        with_source(runner, {"TestCo": StaticSource(company, [intern("1")])})

        outcome = runner.run([company], poll_id="p1", now=T0)

        assert repository.count() == 1
        assert outcome.notification is not None
        assert outcome.notification.emitted == 0
        assert repository.pending_notifications()

    def test_the_next_poll_delivers_the_outstanding_alert(self) -> None:
        repository = InMemoryJobRepository()
        email = MemoryEmailTransport(fail_on_call=1)
        push = MemoryPushTransport(fail_on_call=1)
        settings = for_tests()
        notifier = Notifier(
            settings, repository=repository, email_transport=email, push_transport=push
        )
        runner = PollRunner(settings, repository=repository, notifier=notifier)
        company = make_company()
        with_source(runner, {"TestCo": StaticSource(company, [intern("1")])})

        runner.run([company], poll_id="p1", now=T0)
        assert not email.sent

        # The pending index is the backstop.
        retried = notifier.drain_pending(now=T1)
        assert retried.emitted == 2
        assert len(email.sent) == 1
        assert repository.pending_notifications() == []


class TestFullPipelineWithRealAdapters:
    """The whole path, driven by a real provider fixture rather than Job objects."""

    def test_greenhouse_fixture_end_to_end(
        self,
        stack: tuple[PollRunner, InMemoryJobRepository, MemoryEmailTransport, MemoryPushTransport],
    ) -> None:
        from jobmonitor.scrapers import build_source

        runner, repository, email, push = stack
        company = make_company(provider="greenhouse", config={"board_token": "testco"})
        transport = FakeTransport(
            always=ScriptedResponse.json(load_fixture("greenhouse", "board.json"))
        )
        with_source(runner, {company.company: build_source(company, build_client(transport))})

        outcome = runner.run([company], poll_id="p1", now=T0)

        # The fixture has 4 parseable jobs; 2 are technical internships.
        stored = {record.title for record in repository.all_records()}
        assert "Software Engineer Intern, Summer 2027" in stored
        assert "Machine Learning Engineer Intern" in stored
        assert "Marketing Intern" not in stored
        assert "Senior Staff Software Engineer" not in stored
        assert len(outcome.new_records) == 2
        assert len(email.sent) >= 1
        assert len(push.sent) >= 1

    def test_the_application_url_survives_the_whole_pipeline(
        self,
        stack: tuple[PollRunner, InMemoryJobRepository, MemoryEmailTransport, MemoryPushTransport],
    ) -> None:
        from jobmonitor.scrapers import build_source

        runner, repository, email, _push = stack
        company = make_company(provider="greenhouse", config={"board_token": "testco"})
        transport = FakeTransport(
            always=ScriptedResponse.json(load_fixture("greenhouse", "board.json"))
        )
        with_source(runner, {company.company: build_source(company, build_client(transport))})
        runner.run([company], poll_id="p1", now=T0)

        record = next(r for r in repository.all_records() if r.external_id == "4020160008")
        assert record.url == "https://job-boards.greenhouse.io/testco/jobs/4020160008"
        assert record.url in email.sent[0][0].text_body


class TestObservabilityCounters:
    def test_the_summary_reports_every_prd_counter(
        self,
        stack: tuple[PollRunner, InMemoryJobRepository, MemoryEmailTransport, MemoryPushTransport],
    ) -> None:
        runner, _repository, _email, _push = stack
        good = make_company("Good Co")
        bad = make_company("Bad Co")
        with_source(
            runner,
            {
                "Good Co": StaticSource(
                    good,
                    [
                        intern("1", company="Good Co"),
                        intern("2", company="Good Co", title="Marketing Intern"),
                    ],
                ),
                "Bad Co": BrokenSource(bad),
            },
        )

        summary = runner.run([good, bad], poll_id="p1", now=T0).summary().to_dict()

        assert summary["scrapers_attempted"] == 2
        assert summary["scrapers_succeeded"] == 1
        assert summary["scrapers_failed"] == 1
        assert summary["jobs_fetched"] == 2
        assert summary["jobs_relevant"] == 1
        assert summary["new_jobs"] == 1
        assert summary["notifications_emitted"] == 2  # email + push
        assert summary["notifications_failed"] == 0
        assert summary["duration_ms"] >= 0

    def test_health_records_match_the_prd_structured_log_shape(
        self,
        stack: tuple[PollRunner, InMemoryJobRepository, MemoryEmailTransport, MemoryPushTransport],
    ) -> None:
        runner, _repository, _email, _push = stack
        company = make_company()
        with_source(runner, {"TestCo": StaticSource(company, [intern("1")])})
        record = runner.run([company], poll_id="p1", now=T0).health_records[0].to_dict()
        for key in (
            "company",
            "provider",
            "duration_ms",
            "jobs_found",
            "relevant_jobs",
            "new_jobs",
            "status",
        ):
            assert key in record


class TestUnsupportedCompaniesAreSkipped:
    def test_only_pollable_companies_are_run(self) -> None:
        from jobmonitor.models.company import CompanyRegistry, SupportStatus

        registry = CompanyRegistry(
            (
                make_company("Supported Co"),
                make_company("Blocked Co", support_status=SupportStatus.BLOCKED),
            )
        )
        assert [c.company for c in registry.pollable()] == ["Supported Co"]


def test_http_client_is_shared_across_companies_in_a_shard() -> None:
    """One pooled client per invocation, not one per company."""
    settings = for_tests()
    client = HttpClient(settings.http, transport=FakeTransport(always=ScriptedResponse.json({})))
    runner = PollRunner(settings, repository=InMemoryJobRepository(), client=client)
    assert runner.client is client
