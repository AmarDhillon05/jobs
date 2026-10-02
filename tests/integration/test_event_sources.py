"""Event sources riding the job pipeline (Level 6).

A company's event sources are polled as extra per-company sources: same name,
own provider, own health row. These tests pin the behaviour that makes that safe:

* an event source failing never touches the job board (and vice versa);
* the first successful poll of a new event source is stored silently - the
  events it lists were already there before monitoring began;
* later polls alert only on events that appeared since, labelled by category;
* a source that keeps failing is polled hourly, not every ten minutes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from tests.conftest import make_company
from tests.integration.test_pipeline import BrokenSource, StaticSource, intern

from jobmonitor.config import FilterSettings, for_tests
from jobmonitor.filtering import JobFilter
from jobmonitor.models.company import Company, EventCategory, EventSource
from jobmonitor.models.health import ScraperHealth, ScraperStatus
from jobmonitor.models.job import EVENT_TYPE, INDUSTRY_EVENT_TYPE, PROGRAM_TYPE, Job
from jobmonitor.notifications import MemoryEmailTransport, MemoryPushTransport, Notifier
from jobmonitor.notifications.digest import DigestSender
from jobmonitor.orchestration import PollRunner
from jobmonitor.orchestration.pipeline import EVENT_SOURCE_COOLDOWN, EVENT_SOURCE_FAILURE_LIMIT
from jobmonitor.scrapers.base import JobSource
from jobmonitor.storage.memory import InMemoryHealthRepository, InMemoryJobRepository

pytestmark = pytest.mark.integration

T0 = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
T1 = T0 + timedelta(minutes=10)
T2 = T0 + timedelta(minutes=20)


def event(
    identifier: str, title: str = "Info Session · Oct 20 · New York", **kwargs: object
) -> Job:
    payload: dict[str, object] = {
        "company": "TestCo",
        "title": title,
        "url": f"https://testco.example/events/{identifier}",
        "source": "event_page",
        "external_id": f"event:{identifier}",
        "location": "New York, NY",
    }
    payload.update(kwargs)
    return Job(**payload)  # type: ignore[arg-type]


def company_with_events(*categories: EventCategory) -> Company:
    sources = tuple(
        EventSource(
            provider="event_page",
            provider_config={"url": f"https://testco.example/{category.value}"},
            category=category,
            name=category.value,
        )
        for category in categories
    )
    return make_company(event_sources=sources)


class Stack:
    def __init__(self) -> None:
        self.settings = for_tests(app_base_url="https://app.test")
        self.repository = InMemoryJobRepository()
        self.health = InMemoryHealthRepository()
        self.email = MemoryEmailTransport()
        self.push = MemoryPushTransport()
        notifier = Notifier(
            self.settings,
            repository=self.repository,
            email_transport=self.email,
            push_transport=self.push,
        )
        self.runner = PollRunner(
            self.settings,
            repository=self.repository,
            notifier=notifier,
            health_repository=self.health,
            job_filter=JobFilter(self.settings.filters),
        )
        #: provider_config["url"] (event sources) or "board" -> jobs it lists.
        self.listings: dict[str, list[Job]] = {}
        self.broken: set[str] = set()
        self.calls: list[str] = []
        self.runner.source_factory = self._source

    def _source(self, target: Company) -> JobSource:
        key = str(target.provider_config.get("url", "board"))
        self.calls.append(key)
        if key in self.broken:
            return BrokenSource(target)
        return StaticSource(target, self.listings.get(key, []))

    def poll(self, company: Company, now: datetime):  # type: ignore[no-untyped-def]
        return self.runner.run([company], poll_id=now.isoformat(), now=now)

    def pushed_titles(self) -> list[str]:
        return [message.title for message, _devices in self.push.sent]


RECRUITING_URL = "https://testco.example/recruiting"
INDUSTRY_URL = "https://testco.example/industry"


@pytest.fixture
def stack() -> Stack:
    return Stack()


class TestExpansion:
    def test_duplicate_source_labels_are_rejected(self) -> None:
        from jobmonitor.models.company import RegistryError

        twin = EventSource(provider="luma", provider_config={"calendar": "x"})
        with pytest.raises(RegistryError, match="distinct names"):
            make_company(event_sources=(twin, twin))
        named = EventSource(provider="luma", provider_config={"calendar": "y"}, name="y")
        assert len(make_company(event_sources=(twin, named)).sources()) == 3

    def test_sources_lists_board_then_each_event_source(self) -> None:
        company = company_with_events(EventCategory.RECRUITING, EventCategory.INDUSTRY)
        board, recruiting, industry = company.sources()
        assert board is company and not board.is_event_source
        assert recruiting.company == industry.company == "TestCo"
        assert recruiting.provider == "event_page"
        assert recruiting.event_category is EventCategory.RECRUITING
        assert industry.event_category is EventCategory.INDUSTRY
        assert recruiting.event_sources == ()
        assert recruiting.key == "TestCo::event_page:recruiting"

    def test_event_sources_survive_the_queue_round_trip(self) -> None:
        company = company_with_events(EventCategory.RECRUITING)
        assert Company.from_dict(company.to_dict()) == company
        copy = company.sources()[1]
        assert Company.from_dict(copy.to_dict()) == copy


class TestQuietFirstPoll:
    def test_first_poll_stores_existing_events_without_alerting(self, stack: Stack) -> None:
        company = company_with_events(EventCategory.RECRUITING)
        stack.listings["board"] = [intern("1")]
        stack.listings[RECRUITING_URL] = [event("a"), event("b")]

        outcome = stack.poll(company, T0)

        assert len(outcome.new_records) == 3
        # The internship alerts; the two events already listed do not.
        assert len(stack.push.sent) == 1
        assert "Software Engineer Intern" in stack.push.sent[0][0].body
        events = [r for r in stack.repository.all_records() if r.employment_type == EVENT_TYPE]
        assert len(events) == 2
        assert all(r.notification_sent and r.baseline for r in events)

    def test_an_event_appearing_later_alerts_once(self, stack: Stack) -> None:
        company = company_with_events(EventCategory.RECRUITING)
        stack.listings[RECRUITING_URL] = [event("a")]
        stack.poll(company, T0)
        assert stack.push.sent == []

        stack.listings[RECRUITING_URL] = [event("a"), event("b", title="Insight Day · Nov 3")]
        outcome = stack.poll(company, T1)
        assert [r.title for r in outcome.new_records] == ["Insight Day · Nov 3"]
        assert len(stack.push.sent) == 1
        new = stack.repository.get(outcome.new_records[0].job_id)
        assert new is not None and new.notification_sent and not new.baseline

        stack.poll(company, T2)
        assert len(stack.push.sent) == 1  # unchanged: no duplicate alert

    def test_a_source_that_failed_first_is_still_quiet_on_its_first_success(
        self, stack: Stack
    ) -> None:
        company = company_with_events(EventCategory.RECRUITING)
        stack.broken.add(RECRUITING_URL)
        stack.poll(company, T0)
        stack.broken.clear()
        stack.listings[RECRUITING_URL] = [event("a")]

        stack.poll(company, T1)

        assert stack.push.sent == []
        record = stack.repository.all_records()[0]
        assert record.baseline

    def test_baseline_events_are_left_out_of_the_digest(self, stack: Stack) -> None:
        company = company_with_events(EventCategory.RECRUITING)
        stack.listings["board"] = [intern("1")]
        stack.listings[RECRUITING_URL] = [event("a")]
        stack.poll(company, T0)

        digest_email = MemoryEmailTransport()
        sender = DigestSender(
            stack.settings, repository=stack.repository, email_transport=digest_email
        )
        jobs = sender.jobs_in(T0 - timedelta(minutes=1), T0 + timedelta(hours=1))
        assert [r.title for r in jobs] == ["Software Engineer Intern"]

    def test_no_health_repository_means_nothing_is_quiet(self) -> None:
        stack = Stack()
        stack.runner.health_repository = None
        stack.listings[RECRUITING_URL] = [event("a")]
        stack.poll(company_with_events(EventCategory.RECRUITING), T0)
        assert len(stack.push.sent) == 1


class TestEventScoringAndLabels:
    def test_event_source_postings_are_tagged_by_category(self, stack: Stack) -> None:
        company = company_with_events(EventCategory.RECRUITING, EventCategory.INDUSTRY)
        stack.runner.quiet_first_event_poll = False
        stack.listings[RECRUITING_URL] = [event("a")]
        stack.listings[INDUSTRY_URL] = [event("b", title="Developer Summit 2026")]

        stack.poll(company, T0)

        by_title = {r.title: r for r in stack.repository.all_records()}
        assert by_title["Info Session · Oct 20 · New York"].employment_type == EVENT_TYPE
        assert by_title["Info Session · Oct 20 · New York"].relevance_score == 80
        assert by_title["Developer Summit 2026"].employment_type == INDUSTRY_EVENT_TYPE
        assert by_title["Developer Summit 2026"].relevance_score == 60

    def test_a_program_keeps_its_tag_on_an_event_source(self, stack: Stack) -> None:
        company = company_with_events(EventCategory.INDUSTRY)
        stack.runner.quiet_first_event_poll = False
        stack.listings[INDUSTRY_URL] = [
            event("p", title="Fellowship", employment_type=PROGRAM_TYPE)
        ]
        stack.poll(company, T0)
        assert stack.repository.all_records()[0].employment_type == PROGRAM_TYPE

    def test_events_with_old_dates_are_not_dropped_by_the_posting_window(
        self, stack: Stack
    ) -> None:
        company = company_with_events(EventCategory.RECRUITING)
        stack.runner.quiet_first_event_poll = False
        stack.listings[RECRUITING_URL] = [event("a", date_posted=T0 - timedelta(days=60))]
        outcome = stack.poll(company, T0)
        assert len(outcome.new_records) == 1

    def test_events_outside_the_us_are_dropped(self, stack: Stack) -> None:
        stack.runner.settings = for_tests(
            app_base_url="https://app.test",
            filters=FilterSettings(max_posting_age_hours=None, us_only=True),
        )
        company = company_with_events(EventCategory.INDUSTRY)
        stack.runner.quiet_first_event_poll = False
        stack.listings[INDUSTRY_URL] = [
            event("paris", title="Meetup", location="Paris, France"),
            event("online", title="Webinar", location=None),
        ]
        outcome = stack.poll(company, T0)
        assert [r.title for r in outcome.new_records] == ["Webinar"]


class TestIsolation:
    def test_broken_event_source_does_not_touch_the_board(self, stack: Stack) -> None:
        company = company_with_events(EventCategory.RECRUITING)
        stack.listings["board"] = [intern("1")]
        stack.broken.add(RECRUITING_URL)

        outcome = stack.poll(company, T0)

        assert len(outcome.new_records) == 1
        assert len(stack.push.sent) == 1
        statuses = {h.provider: h.status for h in outcome.health_records}
        # The test board's adapter reports itself as "static"; the event source's
        # row is named by its label, so two event_page sources never share a row.
        assert statuses == {
            "static": ScraperStatus.SUCCESS,
            "event_page:recruiting": ScraperStatus.FAILED,
        }

    def test_broken_board_does_not_stop_its_event_sources(self, stack: Stack) -> None:
        company = company_with_events(EventCategory.RECRUITING)
        stack.broken.add("board")
        stack.runner.quiet_first_event_poll = False
        stack.listings[RECRUITING_URL] = [event("a")]

        outcome = stack.poll(company, T0)

        assert len(outcome.new_records) == 1
        assert len(outcome.failures) == 1


class TestCoolDown:
    def test_failure_history_is_carried_between_polls(self, stack: Stack) -> None:
        company = company_with_events(EventCategory.RECRUITING)
        stack.broken.add(RECRUITING_URL)
        for minutes in range(3):
            stack.poll(company, T0 + timedelta(minutes=10 * minutes))
        row = stack.health.get(company.sources()[1].key)
        assert row is not None
        assert row.consecutive_failures == 3
        assert not row.ever_succeeded

    def test_repeatedly_failing_source_is_polled_hourly(self, stack: Stack) -> None:
        company = company_with_events(EventCategory.RECRUITING)
        stack.broken.add(RECRUITING_URL)
        for n in range(EVENT_SOURCE_FAILURE_LIMIT):
            stack.poll(company, T0 + timedelta(minutes=10 * n))
        last_failure = T0 + timedelta(minutes=10 * (EVENT_SOURCE_FAILURE_LIMIT - 1))
        stack.calls.clear()

        stack.poll(company, last_failure + timedelta(minutes=10))
        assert stack.calls == ["board"]  # the event source sat this poll out

        stack.poll(company, last_failure + EVENT_SOURCE_COOLDOWN)
        assert stack.calls == ["board", "board", RECRUITING_URL]

    def test_recovery_resets_the_counter(self, stack: Stack) -> None:
        company = company_with_events(EventCategory.RECRUITING)
        key = company.sources()[1].key
        stack.health.record(
            ScraperHealth(
                company="TestCo",
                provider="event_page:recruiting",
                status=ScraperStatus.FAILED,
                timestamp=T0 - EVENT_SOURCE_COOLDOWN,
                consecutive_failures=5,
                ever_succeeded=True,
            )
        )
        stack.listings[RECRUITING_URL] = [event("a")]

        stack.poll(company, T0)

        row = stack.health.get(key)
        assert row is not None and row.ok and row.consecutive_failures == 0
        # It had succeeded before, so the new event is news, not a baseline.
        assert len(stack.push.sent) == 1


class TestRecordBaselinePersistence:
    def test_baseline_survives_the_item_round_trip(self, stack: Stack) -> None:
        stack.repository.upsert(event("a"), now=T0)
        record = stack.repository.all_records()[0]
        stack.repository.mark_notified([record.job_id], now=T0, baseline=True)
        stored = stack.repository.get(record.job_id)
        assert stored is not None and stored.baseline
        from jobmonitor.models.record import JobRecord

        assert JobRecord.from_item(stored.to_item()).baseline
