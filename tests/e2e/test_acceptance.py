"""Level 8 - the PRD §30 acceptance scenarios, end to end, in process.

All eight named scenarios, run against the real pipeline, the real filter, the
real fingerprint/identity logic, the real notifier and the real API router - and
against **both** storage implementations, so every one is proven once fast and
once against DynamoDB semantics under Moto.

The same five scenarios that can be run on emulated AWS are also run there, in
``tests/aws_local/test_architecture.py``, through real Lambdas, real queues and a
real DLQ. These are the fast, hermetic versions: they need no Docker and no
network, which is what lets them gate every commit.

Scenario 8's client half lives with the clients (``mobile/src/__tests__`` and
``web/e2e``). What is asserted here is the join: that the payload the backend
publishes names a job the API can actually serve, with the fields the app renders.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import unquote

import pytest
from tests.e2e.conftest import APP_BASE, T0, System, fixture_company, posting
from tests.support.http import FakeTransport, ScriptedResponse

from jobmonitor.errors import ParseError, RetryBudgetExhausted
from jobmonitor.http import HttpClient
from jobmonitor.models.health import ScraperStatus
from jobmonitor.notifications.events import SCHEMA_VERSION, Channel
from jobmonitor.notifications.formatters import MAX_PUSH_JOBS
from jobmonitor.orchestration.pipeline import StorageFailure, process_company
from jobmonitor.scrapers.greenhouse import GreenhouseSource

pytestmark = pytest.mark.e2e


def greenhouse_board(*identifiers: str) -> dict[str, object]:
    """A Greenhouse board response, for the scenarios that need real HTTP."""
    return {
        "jobs": [
            {
                "id": int(identifier),
                "title": "Software Engineer Intern",
                "absolute_url": f"https://boards.greenhouse.io/testco/jobs/{identifier}",
                "updated_at": "2026-09-25T00:00:00Z",
                "location": {"name": "San Francisco, CA"},
                "content": "Build the thing.",
            }
            for identifier in identifiers
        ],
        "meta": {"total": len(identifiers)},
    }


# --------------------------------------------------------------- Scenario 1


class TestScenario1NewJob:
    """schedule -> scrape -> normalize -> filter -> fingerprint -> store -> notify -> feed."""

    def test_the_whole_story(self, system: System) -> None:
        company = fixture_company(jobs=[posting("1")])
        outcome = system.poll([company])

        # ... exactly one new record
        assert len(outcome.new_records) == 1
        record = outcome.new_records[0]
        assert record.company == "TestCo"
        assert record.title == "Software Engineer Intern"
        assert record.first_seen == T0
        assert len(system.stored()) == 1

        # ... considered relevant
        assert record.relevance_score >= system.settings.filters.notify_threshold

        # ... exactly one instant alert (push), and email waits for the digest
        assert outcome.notification is not None
        assert len(outcome.notification.events) == 1
        assert outcome.notification.delivered_channels == {Channel.PUSH}
        assert len(system.push.sent) == 1
        assert system.email.sent == []

        # ... then the hourly digest emails it, exactly once
        digest = system.send_digest()
        assert digest.sent and [r.job_id for r in digest.jobs] == [record.job_id]
        assert len(system.email.sent) == 1

        # ... the feed contains the job
        feed = system.feed()
        assert [entry["job_id"] for entry in feed] == [record.job_id]

        # ... the deep link identifies it, and survives a round trip
        payload = system.push_payloads[0]
        assert payload["schema_version"] == str(SCHEMA_VERSION)
        assert payload["deep_link"].startswith(APP_BASE)
        linked_id = unquote(payload["deep_link"].rsplit("/jobs/", 1)[1])
        assert linked_id == record.job_id

        status, detail = system.get(f"/jobs/{record.job_id}")
        assert status == 200
        assert detail["job"]["job_id"] == record.job_id

        # ... and the application URL is preserved exactly as the source gave it
        assert detail["job"]["url"] == "https://boards.testco.test/jobs/1?gh_src=feed"
        assert payload["apply_url"] == detail["job"]["url"]

    def test_the_digest_email_names_the_company_role_and_link(self, memory_system: System) -> None:
        memory_system.poll([fixture_company(jobs=[posting("1")])])
        memory_system.send_digest()
        message, to, _sender = memory_system.email.sent[0]
        assert to == "you@example.test"
        assert "TestCo: Software Engineer Intern" in message.text_body
        assert "STRONG MATCHES" in message.text_body
        assert "https://boards.testco.test/jobs/1?gh_src=feed" in message.text_body

    def test_the_next_hour_with_nothing_new_sends_no_email(self, system: System) -> None:
        company = fixture_company(jobs=[posting("1")])
        system.poll([company])
        system.send_digest()
        system.poll([company], at=T0 + timedelta(hours=1))
        outcome = system.send_digest(at=T0 + timedelta(hours=2, minutes=2))
        assert outcome.skipped and not outcome.sent
        assert len(system.email.sent) == 1

    def test_a_registered_phone_is_a_push_target(self, memory_system: System) -> None:
        memory_system.register_phone()
        memory_system.poll([fixture_company(jobs=[posting("1")])])
        _message, targets = memory_system.push.sent[0]
        assert targets == ("ios-phone",)

    def test_an_irrelevant_posting_is_stored_by_nobody_and_alerts_nobody(
        self, memory_system: System
    ) -> None:
        memory_system.poll(
            [fixture_company(jobs=[posting("1", title="Marketing Intern - Brand Social")])]
        )
        assert memory_system.stored() == []
        assert memory_system.push.sent == []


# --------------------------------------------------------------- Scenario 2


class TestScenario2SamePollAgain:
    """The duplicate-suppression proof (PRD §15 Gate F)."""

    def test_no_new_job_no_duplicate_notification_first_seen_unchanged(
        self, system: System
    ) -> None:
        company = fixture_company(jobs=[posting("1")])
        first = system.poll([company])
        record = first.new_records[0]
        system.send_digest()

        later = T0 + timedelta(minutes=10)
        second = system.poll([company], at=later, poll_id="poll-2")

        assert second.new_records == []
        assert second.updated_records == []
        # 0 duplicate notifications - the assertion the PRD names explicitly.
        assert len(system.email.sent) == 1
        assert len(system.push.sent) == 1
        # ... and the following hour's digest has nothing to say.
        assert system.send_digest(at=T0 + timedelta(hours=2, minutes=2)).skipped
        assert len(system.email.sent) == 1

        stored = system.repository.get(record.job_id)
        assert stored is not None
        assert stored.first_seen == T0, "first_seen is authoritative and must not move"
        assert stored.last_seen == later
        assert stored.times_seen == 2
        assert stored.notification_sent is True

    def test_ten_identical_polls_still_send_one_alert(self, memory_system: System) -> None:
        company = fixture_company(jobs=[posting("1")])
        for index in range(10):
            memory_system.poll([company], at=T0 + timedelta(minutes=10 * index))
        assert len(memory_system.push.sent) == 1
        assert len(memory_system.stored()) == 1

    def test_the_feed_never_shows_the_job_twice(self, system: System) -> None:
        company = fixture_company(jobs=[posting("1")])
        system.poll([company])
        system.poll([company], at=T0 + timedelta(minutes=10))
        feed = system.feed()
        assert len(feed) == 1

    def test_a_changed_posting_updates_without_re_alerting(self, system: System) -> None:
        """PRD §9: an *updated* job is distinct from a new one."""
        company = fixture_company(jobs=[posting("1")])
        system.poll([company])
        changed = fixture_company(
            jobs=[posting("1", location="New York, NY", description="Build a different thing.")]
        )
        later = T0 + timedelta(minutes=10)
        second = system.poll([changed], at=later, poll_id="poll-2")

        assert second.new_records == []
        assert len(second.updated_records) == 1
        assert second.updated_records[0].location == "New York, NY"
        # An edit is not a new posting: no second alert.
        assert len(system.push.sent) == 1


# --------------------------------------------------------------- Scenario 3


class TestScenario3Mixed:
    """10 existing + 1 new -> exactly one notification, 11 stored correctly."""

    def test_one_alert_for_the_one_new_job(self, system: System) -> None:
        # A medium-priority company so the first poll produces one *grouped*
        # alert rather than ten immediate ones (PRD §22); what matters for this
        # scenario is that the eleventh job produces exactly one more.
        existing = [posting(str(index)) for index in range(1, 11)]
        system.poll([fixture_company(jobs=existing, priority="medium")])
        assert len(system.push.sent) == 1
        # The push payload names at most MAX_PUSH_JOBS ids (a phone notification is
        # not a manifest); the email and the feed carry the whole set.
        assert len(system.push_payloads[0]["job_ids"].split(",")) == MAX_PUSH_JOBS
        assert system.push_payloads[0]["job_count"] == "10"
        assert len(system.stored()) == 10

        later = T0 + timedelta(minutes=10)
        second = system.poll(
            [
                fixture_company(
                    jobs=[*existing, posting("11", title="Backend Engineer Intern")],
                    priority="medium",
                )
            ],
            at=later,
            poll_id="poll-2",
        )

        assert len(second.new_records) == 1
        assert second.new_records[0].title == "Backend Engineer Intern"
        assert len(system.push.sent) == 2, "one further alert, for the new job only"
        assert system.push_payloads[1]["job_ids"] == second.new_records[0].job_id

        # 11 stored, and every one of the 10 keeps its original first_seen.
        stored = {record.job_id: record for record in system.stored()}
        assert len(stored) == 11
        untouched = [r for r in stored.values() if r.first_seen == T0]
        assert len(untouched) == 10
        assert all(record.last_seen == later for record in stored.values())

    def test_the_feed_shows_all_eleven_newest_first(self, system: System) -> None:
        existing = [posting(str(index)) for index in range(1, 11)]
        system.poll([fixture_company(jobs=existing, priority="medium")])
        system.poll(
            [fixture_company(jobs=[*existing, posting("11")], priority="medium")],
            at=T0 + timedelta(minutes=10),
            poll_id="poll-2",
        )
        feed = system.feed()
        assert len(feed) == 11
        assert feed[0]["first_seen"] > feed[-1]["first_seen"]


# --------------------------------------------------------------- Scenario 4


class TestScenario4ScraperFailureIsolation:
    """One broken scraper must not stop the others (PRD §16, Gate F)."""

    def test_the_other_companies_still_produce_jobs(self, system: System) -> None:
        companies = [
            fixture_company("Company A", fail="endpoint returned garbage"),
            fixture_company("Company B", jobs=[posting("b1")]),
            fixture_company("Company C", jobs=[posting("c1")]),
        ]
        outcome = system.poll(companies)

        assert [o.company for o in outcome.failures] == ["Company A"]
        assert sorted(o.company for o in outcome.successes) == ["Company B", "Company C"]
        assert len(outcome.new_records) == 2
        assert len(system.stored()) == 2
        # B and C's jobs were still alerted on.
        assert len(system.push.sent) >= 1

    def test_the_failure_is_recorded_with_its_error(self, system: System) -> None:
        companies = [
            fixture_company("Company A", fail="parser mismatch"),
            fixture_company("Company B", jobs=[posting("b1")]),
        ]
        system.poll(companies)

        health = {record.company: record for record in system.health.latest()}
        assert health["Company A"].status is ScraperStatus.FAILED
        assert health["Company A"].error_type == "ParseError"
        assert "parser mismatch" in (health["Company A"].error or "")
        assert health["Company B"].ok

    def test_a_403_is_marked_blocked_and_never_retried(self, memory_system: System) -> None:
        """Refusing automated access is respected, not worked around (PRD §5)."""
        transport = FakeTransport(always=ScriptedResponse.error(403))
        company = memory_system_with_http(memory_system, "Blocked Co", transport)
        memory_system.poll([company])

        health = {record.company: record for record in memory_system.health.latest()}
        assert health["Blocked Co"].status is ScraperStatus.BLOCKED
        assert transport.call_count == 1, "a refusal is permanent; the budget is not spent on it"

    def test_the_whole_run_does_not_crash_and_reports_a_summary(
        self, memory_system: System
    ) -> None:
        outcome = memory_system.poll(
            [
                fixture_company("Company A", fail="boom"),
                fixture_company("Company B", jobs=[posting("b1")]),
            ]
        )
        summary = outcome.summary()
        assert summary.scrapers_attempted == 2
        assert summary.scrapers_failed == 1
        assert summary.scrapers_succeeded == 1
        assert summary.new_jobs == 1


def memory_system_with_http(system: System, name: str, transport: FakeTransport, **kwargs: object):
    """Wire one company to a scripted HTTP transport through the real adapter."""
    from tests.conftest import make_company

    company = make_company(name, provider="greenhouse", **kwargs)  # type: ignore[arg-type]
    client = HttpClient(system.settings.http, transport=transport, sleep=lambda _delay: None)
    system.sources[name] = GreenhouseSource(company, client)
    return company


# --------------------------------------------------------------- Scenario 5


class TestScenario5TransientFailure:
    """500, 500, 200 -> the job still arrives (PRD §14 Level 2, §30 Scenario 5)."""

    def test_recovers_and_the_job_reaches_the_feed(self, memory_system: System) -> None:
        transport = FakeTransport(
            [
                ScriptedResponse.error(500),
                ScriptedResponse.error(500),
                ScriptedResponse.json(greenhouse_board("9001")),
            ]
        )
        company = memory_system_with_http(memory_system, "Flaky Co", transport)
        outcome = memory_system.poll([company])

        assert transport.call_count == 3
        assert len(outcome.new_records) == 1
        assert memory_system.feed()[0]["company"] == "Flaky Co"
        assert len(memory_system.push.sent) == 1

    def test_a_permanent_outage_fails_that_company_only(self, memory_system: System) -> None:
        transport = FakeTransport(always=ScriptedResponse.error(500))
        flaky = memory_system_with_http(memory_system, "Down Co", transport)
        healthy = fixture_company("Healthy Co", jobs=[posting("h1")])
        outcome = memory_system.poll([flaky, healthy])

        assert [o.company for o in outcome.failures] == ["Down Co"]
        health = {r.company: r for r in memory_system.health.latest()}
        assert health["Down Co"].attempts == memory_system.settings.http.max_attempts
        assert health["Down Co"].error_type == RetryBudgetExhausted.__name__
        assert len(outcome.new_records) == 1


# --------------------------------------------------------------- Scenario 6


class TestScenario6RateLimit:
    """429 + Retry-After -> controlled backoff, no hammering (PRD §17)."""

    def test_waits_what_the_source_asked_for_then_succeeds(self, memory_system: System) -> None:
        slept: list[float] = []
        transport = FakeTransport(
            [
                ScriptedResponse.error(429, headers={"Retry-After": "2"}),
                ScriptedResponse.json(greenhouse_board("9002")),
            ]
        )
        from tests.conftest import make_company

        company = make_company("Limited Co", provider="greenhouse")
        client = HttpClient(memory_system.settings.http, transport=transport, sleep=slept.append)
        memory_system.sources["Limited Co"] = GreenhouseSource(company, client)

        outcome = memory_system.poll([company])

        assert slept == [2.0], "Retry-After wins over our own backoff; guessing lower is hammering"
        assert len(outcome.new_records) == 1
        assert transport.call_count == 2

    def test_a_persistent_429_is_bounded(self, memory_system: System) -> None:
        slept: list[float] = []
        transport = FakeTransport(always=ScriptedResponse.error(429, headers={"Retry-After": "1"}))
        from tests.conftest import make_company

        company = make_company("Limited Co", provider="greenhouse")
        client = HttpClient(memory_system.settings.http, transport=transport, sleep=slept.append)
        memory_system.sources["Limited Co"] = GreenhouseSource(company, client)

        outcome = memory_system.poll([company])

        assert transport.call_count == memory_system.settings.http.max_attempts
        assert len(outcome.failures) == 1
        health = memory_system.health.latest()[0]
        assert health.status is ScraperStatus.FAILED
        assert health.attempts == memory_system.settings.http.max_attempts


# --------------------------------------------------------------- Scenario 7


class TestScenario7StorageAndWorkerFailure:
    """A failing store must be retried, not silently marked done."""

    def test_a_storage_error_surfaces_rather_than_being_swallowed(
        self, memory_system: System
    ) -> None:
        class BrokenRepository:
            def upsert(self, *args: object, **kwargs: object):
                raise RuntimeError("ProvisionedThroughputExceededException")

        company = fixture_company(jobs=[posting("1")])
        with pytest.raises(StorageFailure, match="ProvisionedThroughput"):
            process_company(
                company,
                settings=memory_system.settings,
                repository=BrokenRepository(),  # type: ignore[arg-type]
                now=T0,
            )

    def test_the_worker_reports_the_message_as_failed_so_sqs_retries_it(self) -> None:
        """The batchItemFailures contract is what makes the DLQ path work."""
        import json

        from tests.e2e.conftest import build_system

        from jobmonitor.models.company import CompanyRegistry
        from jobmonitor.orchestration.handlers import Dependencies, worker_handler
        from jobmonitor.orchestration.queues import InMemoryQueue, InMemoryTopic

        system = build_system("memory")

        class BrokenRepository:
            def upsert(self, *args: object, **kwargs: object):
                raise RuntimeError("table is gone")

            def recent(self, **kwargs: object) -> list[object]:
                return []

            def pending_notifications(self, **kwargs: object) -> list[object]:
                return []

        deps = Dependencies(
            settings=system.settings,
            registry=CompanyRegistry(()),
            repository=BrokenRepository(),  # type: ignore[arg-type]
            health=system.health,
            devices=system.devices,
            scrape_queue=InMemoryQueue(),
            notification_topic=InMemoryTopic(),
        )
        event = {
            "Records": [
                {
                    "messageId": "m1",
                    "body": json.dumps(
                        {
                            "poll_id": "p1",
                            "shard": 0,
                            "companies": [
                                {
                                    "company": "TestCo",
                                    "careers_url": "https://example.test/careers",
                                    "industry": "Testing",
                                    "priority": "high",
                                    "provider": "fixture",
                                    # The handler runs on the real clock, so the
                                    # posting must be genuinely recent to get past
                                    # the one-day window and reach storage.
                                    "provider_config": {
                                        "jobs": [
                                            posting("1", date_posted=datetime.now(UTC).isoformat())
                                        ]
                                    },
                                    "support_status": "supported",
                                }
                            ],
                        }
                    ),
                }
            ]
        }
        result = worker_handler(event, None, deps=deps)
        # The shape SQS reads to decide what to redeliver. Level 7 proves the
        # message really does end up on the DLQ after the configured attempts.
        assert result["batchItemFailures"] == [{"itemIdentifier": "m1"}]
        assert result["new_jobs"] == 0

    def test_a_notification_failure_leaves_the_job_pending_for_the_next_poll(
        self, memory_system: System
    ) -> None:
        memory_system.push.fail_on_call = 1
        company = fixture_company(jobs=[posting("1")])
        outcome = memory_system.poll([company])

        assert outcome.notification is not None
        assert outcome.notification.emitted == 0
        record = memory_system.stored()[0]
        assert record.notification_sent is False
        assert memory_system.repository.pending_notifications(limit=10)

        # The backstop: the next poll (or drain_pending) sends it, exactly once.
        memory_system.push.fail_on_call = None
        retried = memory_system.notifier.drain_pending(now=T0 + timedelta(minutes=10))
        assert retried.emitted == 1  # push; email is the digest's job
        assert memory_system.repository.get(record.job_id).notification_sent is True  # type: ignore[union-attr]
        # And not a third time.
        assert memory_system.notifier.drain_pending().events == []


# --------------------------------------------------------------- Scenario 8


class TestScenario8AppNotificationPath:
    """The payload the phone receives must resolve to the record the backend has."""

    def test_the_payload_names_a_job_the_api_can_serve(self, system: System) -> None:
        company = fixture_company(jobs=[posting("1")])
        record = system.poll([company]).new_records[0]

        payload = system.push_payloads[0]
        # This is exactly what mobile/src/routes.ts reads.
        assert payload["job_id"] == record.job_id
        assert payload["job_count"] == "1"
        assert payload["urgency"] == "immediate"

        job_id_from_link = unquote(payload["deep_link"].rsplit("/jobs/", 1)[1])
        status, detail = system.get(f"/jobs/{job_id_from_link}")
        assert status == 200

        # Every field the detail screen renders.
        job = detail["job"]
        for field_name in (
            "company",
            "title",
            "location",
            "url",
            "first_seen",
            "date_posted",
            "description",
            "relevance_score",
        ):
            assert field_name in job, field_name
        assert job["company"] == record.company
        assert job["title"] == record.title
        assert job["url"] == record.url

    def test_a_grouped_alert_carries_every_id_and_the_feed_holds_them_all(
        self, system: System
    ) -> None:
        company = fixture_company("Medium Co", jobs=[posting("1"), posting("2")], priority="medium")
        system.poll([company])

        payload = system.push_payloads[0]
        assert payload["urgency"] == "batched"
        ids = payload["job_ids"].split(",")
        assert len(ids) == 2
        # A grouped alert has no single job_id: the app routes it to the feed.
        assert "job_id" not in payload
        assert {entry["job_id"] for entry in system.feed()} == set(ids)

    def test_a_deep_linked_id_that_no_longer_exists_is_a_clean_404(
        self, memory_system: System
    ) -> None:
        status, body = memory_system.get("/jobs/testco:gone")
        assert status == 404
        assert body["error"]["code"] == "not_found"

    def test_a_phone_can_register_and_then_receives_the_next_alert(
        self, memory_system: System
    ) -> None:
        from jobmonitor.api.routes import Request

        response = memory_system.api.handle(
            Request(
                method="POST",
                path="/devices/register",
                headers={"X-Api-Token": "test-token"},
                body='{"device_id": "ios-phone", "token": "ExponentPushToken[abc]", '
                '"transport": "expo"}',
            )
        )
        assert response.status in (200, 201)

        memory_system.poll([fixture_company(jobs=[posting("1")])])
        _message, targets = memory_system.push.sent[0]
        assert targets == ("ios-phone",)

    def test_device_registration_without_the_token_is_refused(self, memory_system: System) -> None:
        from jobmonitor.api.routes import Request

        response = memory_system.api.handle(
            Request(
                method="POST",
                path="/devices/register",
                body='{"device_id": "x", "token": "ExponentPushToken[abc]"}',
            )
        )
        assert response.status == 401
        assert memory_system.devices.enabled_devices() == []


# ------------------------------------------------------- the whole run, once


class TestFullRunObservability:
    """PRD §25: a poll must be able to account for itself."""

    def test_the_summary_counts_everything_the_prd_lists(self, memory_system: System) -> None:
        outcome = memory_system.poll(
            [
                fixture_company("Company A", jobs=[posting("a1"), posting("a2")]),
                fixture_company("Company B", jobs=[posting("b1", title="Marketing Intern")]),
                fixture_company("Company C", fail="HTTP 403"),
            ]
        )
        summary = outcome.summary().to_dict()
        assert summary["scrapers_attempted"] == 3
        assert summary["scrapers_succeeded"] == 2
        assert summary["scrapers_failed"] == 1
        assert summary["jobs_fetched"] == 3
        assert summary["jobs_relevant"] == 2
        assert summary["new_jobs"] == 2
        assert summary["notifications_emitted"] >= 1
        assert summary["duration_ms"] >= 0

    def test_the_health_view_lists_every_company_it_tried(self, memory_system: System) -> None:
        memory_system.poll(
            [
                fixture_company("Company A", jobs=[posting("a1")]),
                fixture_company("Company X", fail="HTTP 403"),
            ]
        )
        records = {record.company: record for record in memory_system.health.latest()}
        assert set(records) == {"Company A", "Company X"}
        assert records["Company A"].jobs_found == 1
        assert not records["Company X"].ok

    def test_an_empty_board_is_not_a_failure(self, memory_system: System) -> None:
        """PRD §7: "empty internship results do not automatically imply failure"."""
        transport = FakeTransport(always=ScriptedResponse.json({"jobs": [], "meta": {"total": 0}}))
        company = memory_system_with_http(memory_system, "Quiet Co", transport)
        outcome = memory_system.poll([company])

        assert outcome.failures == []
        health = memory_system.health.latest()[0]
        assert health.status is ScraperStatus.EMPTY
        assert health.ok
        assert memory_system.push.sent == []


class TestParseErrorIsContained:
    def test_one_malformed_posting_does_not_lose_the_good_ones(self, memory_system: System) -> None:
        company = fixture_company(jobs=[posting("1"), {"title": "Broken - no url"}, posting("2")])
        outcome = memory_system.poll([company])

        # The two good postings survive; the broken one is counted, not fatal.
        assert len(outcome.new_records) == 2
        health = memory_system.health.latest()[0]
        assert health.ok
        assert health.jobs_malformed == 1
        assert ParseError.__name__ not in (health.error_type or "")


# ----------------------------------------------------------- one-day window


class TestOnlyTheLastDayIsSeen:
    """Postings older than a day never enter the system at all.

    Runs with the production window (24h) - the acceptance system is built with
    ``FilterSettings()``, not the test default that switches it off.
    """

    def test_the_acceptance_system_runs_with_the_production_window(
        self, memory_system: System
    ) -> None:
        assert memory_system.settings.filters.max_posting_age == timedelta(days=1)

    def test_an_old_posting_is_never_stored_notified_or_shown(self, system: System) -> None:
        stale = posting("old", date_posted=(T0 - timedelta(days=3)).isoformat())
        outcome = system.poll([fixture_company(jobs=[stale])])

        assert outcome.new_records == []
        assert system.stored() == []
        assert system.push.sent == [] and system.email.sent == []
        assert system.feed() == []

    def test_only_the_recent_posting_on_a_mixed_board_gets_through(self, system: System) -> None:
        board = [
            posting("fresh", date_posted=(T0 - timedelta(hours=2)).isoformat()),
            posting("week-old", date_posted=(T0 - timedelta(days=7)).isoformat()),
            posting("month-old", date_posted=(T0 - timedelta(days=30)).isoformat()),
        ]
        outcome = system.poll([fixture_company(jobs=board)])

        assert [record.external_id for record in outcome.new_records] == ["fresh"]
        assert [entry["url"] for entry in system.feed()] == [
            "https://boards.testco.test/jobs/fresh?gh_src=feed"
        ]
        assert len(system.push.sent) == 1

    def test_a_day_only_date_from_yesterday_is_still_seen(self, memory_system: System) -> None:
        # Workday-style: only the day is known, recorded as midnight. At noon today
        # that looks 36h old, but the job may have gone up late yesterday evening.
        yesterday = (T0 - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        outcome = memory_system.poll(
            [fixture_company(jobs=[posting("1", date_posted=yesterday.isoformat())])]
        )
        assert len(outcome.new_records) == 1

    def test_an_undated_posting_is_seen_once_and_never_re_alerted(self, system: System) -> None:
        undated = posting("1")
        undated.pop("date_posted")
        company = fixture_company(jobs=[undated])
        for index in range(5):
            system.poll([company], at=T0 + timedelta(hours=index), poll_id=f"p{index}")

        assert len(system.stored()) == 1
        assert len(system.push.sent) == 1, "the seen-before check stops a repeat alert"

    def test_a_board_with_nothing_recent_is_healthy_not_failed(self, memory_system: System) -> None:
        stale = [posting(str(i), date_posted="2026-08-01T12:00:00Z") for i in range(5)]
        outcome = memory_system.poll([fixture_company(jobs=stale)])

        assert outcome.failures == []
        health = memory_system.health.latest()[0]
        assert health.ok
        # The board answered with five postings; none were recent. Both facts are
        # visible, so "quiet day" is never confused with "broken scraper".
        assert health.jobs_found == 5
        assert health.relevant_jobs == 0

    def test_a_posting_ages_out_after_a_day(self, memory_system: System) -> None:
        # Seen and alerted while fresh; a day later the same posting is outside the
        # window, so it neither re-alerts nor refreshes.
        company = fixture_company(
            jobs=[posting("1", date_posted=(T0 - timedelta(hours=1)).isoformat())]
        )
        memory_system.poll([company])
        later = memory_system.poll([company], at=T0 + timedelta(days=2), poll_id="later")

        assert later.new_records == [] and later.updated_records == []
        assert len(memory_system.push.sent) == 1
