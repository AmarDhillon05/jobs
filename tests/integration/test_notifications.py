"""Level 4 - notifications (PRD §14 Level 4, §22).

Covers the required list: correct payload, email formatter, push formatter,
multiple jobs, deep link, failed delivery handling, and duplicate-notification
prevention. Nothing is actually delivered anywhere - every transport is a fake,
which is exactly what PRD §22 asks for.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from tests.conftest import make_company

from jobmonitor.config import EmailSettings, FilterSettings, PushSettings, for_tests
from jobmonitor.models.job import Job
from jobmonitor.models.record import DeviceRegistration, JobRecord
from jobmonitor.notifications import (
    SCHEMA_VERSION,
    Channel,
    NotificationEvent,
    Notifier,
    Urgency,
    format_email,
    format_push,
)
from jobmonitor.notifications.formatters import MAX_PUSH_BODY_CHARS
from jobmonitor.notifications.transports import (
    ConsoleEmailTransport,
    ConsolePushTransport,
    DeliveryError,
    MemoryEmailTransport,
    MemoryPushTransport,
    SesEmailTransport,
    SnsPushTransport,
    TransportUnavailable,
    WebPushTransport,
    build_email_transport,
    build_push_transport,
)
from jobmonitor.storage.memory import InMemoryDeviceRepository, InMemoryJobRepository

pytestmark = pytest.mark.integration

T0 = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
APP = "https://app.test"


def make_job(identifier: str = "1", **kwargs: object) -> Job:
    payload: dict[str, object] = {
        "company": "Stripe",
        "title": "Software Engineer Intern",
        "url": f"https://stripe.com/jobs/listing/swe-intern/{identifier}",
        "source": "greenhouse",
        "external_id": identifier,
        "location": "San Francisco",
        "date_posted": "2026-09-18T00:00:00Z",
        "description": "Work on payments infrastructure.",
    }
    payload.update(kwargs)
    return Job(**payload)  # type: ignore[arg-type]


def make_record(identifier: str = "1", *, priority: str = "high", **kwargs: object) -> JobRecord:
    record = JobRecord.from_job(
        make_job(identifier, **kwargs),
        relevance_score=73,
        company=make_company(priority=priority),  # type: ignore[arg-type]
        now=T0,
    )
    return record


def event_for(*records: JobRecord, urgency: Urgency = Urgency.IMMEDIATE) -> NotificationEvent:
    return NotificationEvent.for_records(list(records), app_base_url=APP, urgency=urgency)


class TestEmailFormatter:
    def test_matches_the_prd_example_layout(self) -> None:
        message = format_email(event_for(make_record()))
        body = message.text_body
        assert "NEW INTERNSHIP" in body
        assert "Company: Stripe" in body
        assert "Role: Software Engineer Intern" in body
        assert "Location: San Francisco" in body
        assert "First Seen: 2026-09-26T12:00:00+00:00" in body
        assert "Posted: 2026-09-18T00:00:00+00:00" in body
        assert "Apply:" in body
        assert "https://stripe.com/jobs/listing/swe-intern/1" in body

    def test_subject_names_the_company_and_role(self) -> None:
        message = format_email(event_for(make_record()))
        assert message.subject == "[Internship] Stripe - Software Engineer Intern"

    def test_missing_posting_date_is_omitted_not_blank(self) -> None:
        message = format_email(event_for(make_record(date_posted=None)))
        assert "Posted:" not in message.text_body

    def test_missing_location_says_so(self) -> None:
        message = format_email(event_for(make_record(location=None)))
        assert "Location: Location not specified" in message.text_body

    def test_the_application_url_is_preserved_exactly(self) -> None:
        url = "https://stripe.com/jobs/listing/x/1?gh_jid=42"
        message = format_email(event_for(make_record(url=url)))
        assert url in message.text_body
        assert url in message.html_body

    def test_deep_link_is_included_for_the_app(self) -> None:
        record = make_record()
        message = format_email(event_for(record))
        assert record.deep_link(APP) in message.text_body

    def test_html_body_escapes_untrusted_content(self) -> None:
        # Titles come from third-party sites; they must not inject markup.
        message = format_email(event_for(make_record(title="Intern <script>alert(1)</script>")))
        assert "<script>" not in message.html_body
        assert "&lt;script&gt;" in message.html_body

    def test_multiple_jobs_are_grouped_into_one_email(self) -> None:
        records = [make_record("1"), make_record("2", title="ML Engineer Intern")]
        message = format_email(event_for(*records, urgency=Urgency.BATCHED))
        assert "2 new internships" in message.subject or "2 new roles" in message.subject
        assert message.text_body.count("NEW INTERNSHIP") == 2
        assert "Software Engineer Intern" in message.text_body
        assert "ML Engineer Intern" in message.text_body

    def test_grouped_subject_lists_companies_and_caps_the_list(self) -> None:
        records = [make_record(str(index), company=f"Company{index}") for index in range(6)]
        message = format_email(event_for(*records, urgency=Urgency.BATCHED))
        assert "+3 more" in message.subject

    def test_relevance_score_is_shown(self) -> None:
        assert "Relevance: 73/100" in format_email(event_for(make_record())).text_body


class TestPushFormatter:
    def test_single_job_payload(self) -> None:
        record = make_record()
        message = format_push(event_for(record))
        assert message.title == "Stripe - new internship"
        assert "Software Engineer Intern" in message.body
        assert message.data["job_id"] == record.job_id
        assert message.data["deep_link"] == record.deep_link(APP)
        assert message.data["apply_url"] == record.url
        assert message.data["schema_version"] == str(SCHEMA_VERSION)

    def test_deep_link_identifies_the_specific_job(self) -> None:
        from urllib.parse import quote, unquote

        record = make_record()
        message = format_push(event_for(record))
        # The id contains a colon, so it is percent-encoded in the path; the
        # client decodes it (see mobile/src/routes).
        assert message.deep_link.endswith(f"/jobs/{quote(record.job_id, safe='')}")
        assert unquote(message.deep_link.rsplit("/", 1)[-1]) == record.job_id

    def test_grouped_payload_points_at_the_feed(self) -> None:
        records = [make_record("1"), make_record("2")]
        message = format_push(event_for(*records, urgency=Urgency.BATCHED))
        assert message.title == "2 new internships"
        assert "/jobs/" not in message.deep_link
        assert message.data["job_count"] == "2"
        assert message.data["job_ids"] == ",".join(r.job_id for r in records)

    def test_body_is_truncated_to_the_push_size_limit(self) -> None:
        records = [
            make_record(str(index), company=f"A Very Long Company Name Number {index}")
            for index in range(10)
        ]
        message = format_push(event_for(*records, urgency=Urgency.BATCHED))
        assert len(message.body) <= MAX_PUSH_BODY_CHARS
        assert message.body.endswith("...")

    def test_payload_values_are_all_strings(self) -> None:
        # SNS message attributes and Web Push data maps both require strings.
        message = format_push(event_for(make_record()))
        assert all(isinstance(value, str) for value in message.data.values())

    def test_urgency_is_carried(self) -> None:
        assert format_push(event_for(make_record())).data["urgency"] == "immediate"


class TestEventSerialisation:
    def test_round_trips_through_json(self) -> None:
        original = event_for(make_record("1"), make_record("2"))
        restored = NotificationEvent.from_json(original.to_json())
        assert restored.job_ids == original.job_ids
        assert restored.urgency is original.urgency
        assert restored.jobs[0].deep_link == original.jobs[0].deep_link

    def test_payload_is_json_serialisable_for_a_queue(self) -> None:
        json.loads(event_for(make_record()).to_json())

    def test_an_event_must_carry_at_least_one_job(self) -> None:
        with pytest.raises(ValueError, match="at least one job"):
            NotificationEvent(jobs=())

    def test_a_payload_without_jobs_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="no jobs"):
            NotificationEvent.from_dict({"jobs": []})

    def test_an_unknown_urgency_falls_back_rather_than_failing(self) -> None:
        payload = event_for(make_record()).to_dict()
        payload["urgency"] = "screaming"
        assert NotificationEvent.from_dict(payload).urgency is Urgency.BATCHED

    def test_an_unreadable_timestamp_falls_back(self) -> None:
        payload = event_for(make_record()).to_dict()
        payload["created_at"] = "not a date"
        assert NotificationEvent.from_dict(payload).created_at is not None


class TestNotifierDelivery:
    def _notifier(self, **kwargs: object) -> tuple[Notifier, InMemoryJobRepository]:
        repository = InMemoryJobRepository()
        settings = for_tests(app_base_url=APP)
        devices = InMemoryDeviceRepository()
        devices.register(
            DeviceRegistration(device_id="phone-1", token="tok", registered_at=T0, last_seen=T0)
        )
        notifier = Notifier(
            settings,
            repository=repository,
            email_transport=kwargs.get("email") or MemoryEmailTransport(),  # type: ignore[arg-type]
            push_transport=kwargs.get("push") or MemoryPushTransport(),  # type: ignore[arg-type]
            device_repository=devices,
        )
        return notifier, repository

    def test_both_channels_are_used(self) -> None:
        email, push = MemoryEmailTransport(), MemoryPushTransport()
        notifier, repository = self._notifier(email=email, push=push)
        record = repository.upsert(make_job(), relevance_score=73, now=T0).record

        outcome = notifier.notify([record], now=T0)

        assert len(email.sent) == 1
        assert len(push.sent) == 1
        assert outcome.delivered_channels == {Channel.EMAIL, Channel.PUSH}
        assert outcome.emitted == 2
        assert outcome.failed == 0

    def test_the_record_is_flagged_as_notified(self) -> None:
        notifier, repository = self._notifier()
        record = repository.upsert(make_job(), now=T0).record
        notifier.notify([record], now=T0)
        stored = repository.get(record.job_id)
        assert stored is not None
        assert stored.notification_sent is True

    def test_push_is_addressed_to_registered_devices(self) -> None:
        push = MemoryPushTransport()
        notifier, repository = self._notifier(push=push)
        record = repository.upsert(make_job(), now=T0).record
        notifier.notify([record], now=T0)
        assert push.sent[0][1] == ("phone-1",)

    def test_no_devices_still_delivers_email(self) -> None:
        email = MemoryEmailTransport()
        repository = InMemoryJobRepository()
        notifier = Notifier(
            for_tests(app_base_url=APP),
            repository=repository,
            email_transport=email,
            push_transport=MemoryPushTransport(),
        )
        record = repository.upsert(make_job(), now=T0).record
        outcome = notifier.notify([record], now=T0)
        assert len(email.sent) == 1
        assert Channel.EMAIL in outcome.delivered_channels


class TestDuplicateSuppression:
    """PRD §9/§30 Scenario 2: the same job must never alert twice."""

    def test_notifying_the_same_record_twice_sends_once(self) -> None:
        repository = InMemoryJobRepository()
        email = MemoryEmailTransport()
        notifier = Notifier(
            for_tests(app_base_url=APP),
            repository=repository,
            email_transport=email,
            push_transport=MemoryPushTransport(),
        )
        first = repository.upsert(make_job(), now=T0).record
        notifier.notify([first], now=T0)

        # Second poll: same job, re-read from storage.
        repository.upsert(make_job(), now=T0 + timedelta(minutes=10))
        again = repository.get(first.job_id)
        assert again is not None
        outcome = notifier.notify([again], now=T0 + timedelta(minutes=10))

        assert len(email.sent) == 1
        assert outcome.events == []
        assert outcome.skipped_job_ids == [first.job_id]

    def test_an_already_notified_record_is_skipped_without_sending(self) -> None:
        repository = InMemoryJobRepository()
        email = MemoryEmailTransport()
        notifier = Notifier(
            for_tests(app_base_url=APP),
            repository=repository,
            email_transport=email,
            push_transport=MemoryPushTransport(),
        )
        record = repository.upsert(make_job(), now=T0).record
        repository.mark_notified([record.job_id], now=T0)
        stored = repository.get(record.job_id)
        assert stored is not None

        outcome = notifier.notify([stored], now=T0)
        assert email.sent == []
        assert outcome.emitted == 0
        assert outcome.skipped_job_ids == [record.job_id]

    def test_drain_pending_delivers_then_stops_delivering(self) -> None:
        repository = InMemoryJobRepository()
        email = MemoryEmailTransport()
        notifier = Notifier(
            for_tests(app_base_url=APP),
            repository=repository,
            email_transport=email,
            push_transport=MemoryPushTransport(),
        )
        repository.upsert(make_job("1"), now=T0)
        repository.upsert(make_job("2"), now=T0)

        first = notifier.drain_pending(now=T0)
        assert len(first.notified_job_ids) == 2
        second = notifier.drain_pending(now=T0)
        assert second.events == []
        assert len(email.sent) == len(first.events)


class TestFailureHandling:
    def _build(self, **kwargs: object) -> tuple[Notifier, InMemoryJobRepository]:
        repository = InMemoryJobRepository()
        notifier = Notifier(
            for_tests(app_base_url=APP),
            repository=repository,
            email_transport=kwargs.get("email") or MemoryEmailTransport(),  # type: ignore[arg-type]
            push_transport=kwargs.get("push") or MemoryPushTransport(),  # type: ignore[arg-type]
        )
        return notifier, repository

    def test_email_failure_does_not_prevent_push(self) -> None:
        email = MemoryEmailTransport(fail_on_call=1)
        push = MemoryPushTransport()
        notifier, repository = self._build(email=email, push=push)
        record = repository.upsert(make_job(), now=T0).record

        outcome = notifier.notify([record], now=T0)

        assert len(push.sent) == 1
        assert outcome.failed == 1
        assert outcome.delivered_channels == {Channel.PUSH}
        # One channel succeeding is enough to consider the job delivered.
        stored = repository.get(record.job_id)
        assert stored is not None
        assert stored.notification_sent is True

    def test_push_failure_does_not_prevent_email(self) -> None:
        email = MemoryEmailTransport()
        push = MemoryPushTransport(fail_on_call=1)
        notifier, repository = self._build(email=email, push=push)
        record = repository.upsert(make_job(), now=T0).record
        outcome = notifier.notify([record], now=T0)
        assert len(email.sent) == 1
        assert outcome.delivered_channels == {Channel.EMAIL}

    def test_total_failure_leaves_the_record_pending_for_a_retry(self) -> None:
        email = MemoryEmailTransport(fail_on_call=1)
        push = MemoryPushTransport(fail_on_call=1)
        notifier, repository = self._build(email=email, push=push)
        record = repository.upsert(make_job(), now=T0).record

        outcome = notifier.notify([record], now=T0)

        assert outcome.emitted == 0
        assert outcome.failed == 2
        stored = repository.get(record.job_id)
        assert stored is not None
        assert stored.notification_sent is False
        assert repository.pending_notifications()  # next poll will retry

    def test_a_retry_after_a_failure_succeeds(self) -> None:
        email = MemoryEmailTransport(fail_on_call=1)
        push = MemoryPushTransport(fail_on_call=1)
        notifier, repository = self._build(email=email, push=push)
        repository.upsert(make_job(), now=T0)

        notifier.drain_pending(now=T0)
        assert not email.sent

        retried = notifier.drain_pending(now=T0 + timedelta(minutes=10))
        assert retried.emitted == 2
        assert len(email.sent) == 1

    def test_an_unexpected_transport_exception_is_contained(self) -> None:
        class Exploding(MemoryEmailTransport):
            def send(self, message: object, *, to: str, sender: str) -> str:  # type: ignore[override]
                raise RuntimeError("transport bug")

        push = MemoryPushTransport()
        notifier, repository = self._build(email=Exploding(), push=push)
        record = repository.upsert(make_job(), now=T0).record
        outcome = notifier.notify([record], now=T0)
        assert len(push.sent) == 1
        assert any(r.error_type == "RuntimeError" for r in outcome.results)

    def test_delivery_result_records_the_error_detail(self) -> None:
        email = MemoryEmailTransport(fail_on_call=1)
        notifier, repository = self._build(email=email)
        record = repository.upsert(make_job(), now=T0).record
        outcome = notifier.notify([record], now=T0)
        failure = next(r for r in outcome.results if not r.delivered)
        assert failure.error_type == "DeliveryError"
        assert "simulated failure" in failure.detail
        assert failure.job_ids == (record.job_id,)


class TestUrgencyPlanning:
    def _notifier(self, **overrides: object) -> tuple[Notifier, InMemoryJobRepository]:
        repository = InMemoryJobRepository()
        settings = for_tests(app_base_url=APP, **overrides)
        return (
            Notifier(
                settings,
                repository=repository,
                email_transport=MemoryEmailTransport(),
                push_transport=MemoryPushTransport(),
            ),
            repository,
        )

    def test_high_priority_jobs_each_get_their_own_immediate_event(self) -> None:
        notifier, _ = self._notifier()
        records = [make_record("1", priority="high"), make_record("2", priority="high")]
        events = notifier.plan(records)
        assert len(events) == 2
        assert all(event.urgency is Urgency.IMMEDIATE for event in events)
        assert all(event.is_single for event in events)

    def test_lower_priority_jobs_are_grouped_into_one_event(self) -> None:
        notifier, _ = self._notifier()
        records = [make_record(str(i), priority="medium") for i in range(4)]
        events = notifier.plan(records)
        assert len(events) == 1
        assert events[0].urgency is Urgency.BATCHED
        assert len(events[0].jobs) == 4

    def test_a_mixed_poll_produces_immediate_plus_one_batch(self) -> None:
        notifier, _ = self._notifier()
        records = [
            make_record("1", priority="high"),
            make_record("2", priority="medium"),
            make_record("3", priority="experimental"),
        ]
        events = notifier.plan(records)
        assert [len(event.jobs) for event in events] == [1, 2]
        assert events[0].urgency is Urgency.IMMEDIATE
        assert events[1].urgency is Urgency.BATCHED

    def test_immediate_priorities_are_configurable(self) -> None:
        notifier, _ = self._notifier(
            filters=FilterSettings(immediate_alert_priorities=("high", "medium"))
        )
        events = notifier.plan([make_record("1", priority="medium")])
        assert events[0].urgency is Urgency.IMMEDIATE

    def test_planning_an_empty_list_produces_nothing(self) -> None:
        notifier, _ = self._notifier()
        assert notifier.plan([]) == []


class TestTransportFactories:
    def test_email_factory_choices(self) -> None:
        assert isinstance(
            build_email_transport(EmailSettings(transport="memory")), MemoryEmailTransport
        )
        assert isinstance(
            build_email_transport(EmailSettings(transport="console")), ConsoleEmailTransport
        )
        assert isinstance(build_email_transport(EmailSettings(transport="ses")), SesEmailTransport)

    def test_push_factory_choices(self) -> None:
        assert isinstance(
            build_push_transport(PushSettings(transport="memory")), MemoryPushTransport
        )
        assert isinstance(
            build_push_transport(PushSettings(transport="console")), ConsolePushTransport
        )
        assert isinstance(
            build_push_transport(PushSettings(transport="sns"), topic_arn="arn:x"), SnsPushTransport
        )
        assert isinstance(build_push_transport(PushSettings(transport="webpush")), WebPushTransport)

    def test_unknown_transports_are_rejected_with_the_valid_options(self) -> None:
        with pytest.raises(TransportUnavailable, match="memory"):
            build_email_transport(EmailSettings(transport="carrier-pigeon"))
        with pytest.raises(TransportUnavailable, match="sns"):
            build_push_transport(PushSettings(transport="telepathy"))

    def test_console_transports_do_not_raise(self, capsys: pytest.CaptureFixture[str]) -> None:
        event = event_for(make_record())
        ConsoleEmailTransport().send(format_email(event), to="a@b.test", sender="c@d.test")
        ConsolePushTransport().send(format_push(event), devices=[])
        captured = capsys.readouterr().out
        assert "NEW INTERNSHIP" in captured
        assert "deep_link" in captured


class TestAwsTransports:
    def test_sns_publish_shape(self) -> None:
        import boto3
        from moto import mock_aws

        with mock_aws():
            sns = boto3.client("sns", region_name="us-east-1")
            topic_arn = sns.create_topic(Name="alerts")["TopicArn"]
            transport = SnsPushTransport(topic_arn, client=sns)
            receipts = transport.send(format_push(event_for(make_record())), devices=[])
            assert receipts and receipts[0]

    def test_sns_without_a_topic_is_unavailable_not_a_crash(self) -> None:
        with pytest.raises(TransportUnavailable, match="NOTIFICATION_TOPIC_ARN"):
            SnsPushTransport(None).send(format_push(event_for(make_record())), devices=[])

    def test_ses_send_email_shape(self) -> None:
        import boto3
        from moto import mock_aws

        with mock_aws():
            ses = boto3.client("ses", region_name="us-east-1")
            ses.verify_email_identity(EmailAddress="alerts@example.com")
            transport = SesEmailTransport(EmailSettings(transport="ses"), client=ses)
            message_id = transport.send(
                format_email(event_for(make_record())),
                to="you@example.com",
                sender="alerts@example.com",
            )
            assert message_id

    def test_ses_failure_becomes_a_delivery_error(self) -> None:
        import boto3
        from moto import mock_aws

        with mock_aws():
            # Unverified sender: SES rejects it, and we must surface that as a
            # DeliveryError so the notifier can fall back to the other channel.
            ses = boto3.client("ses", region_name="us-east-1")
            transport = SesEmailTransport(EmailSettings(transport="ses"), client=ses)
            with pytest.raises(DeliveryError, match="SES send_email failed"):
                transport.send(
                    format_email(event_for(make_record())),
                    to="you@example.com",
                    sender="unverified@example.com",
                )

    def test_webpush_without_the_optional_extra_says_what_to_do(self) -> None:
        transport = WebPushTransport(PushSettings(transport="webpush", vapid_private_key="k"))
        devices = [DeviceRegistration(device_id="d", token="{}")]
        try:
            transport.send(format_push(event_for(make_record())), devices=devices)
        except TransportUnavailable as exc:
            assert "pip install" in str(exc)
            assert "PUSH_TRANSPORT=sns" in str(exc)
        except DeliveryError:
            pytest.skip("pywebpush is installed in this environment")

    def test_webpush_requires_a_vapid_key(self) -> None:
        transport = WebPushTransport(PushSettings(transport="webpush"))
        with pytest.raises(TransportUnavailable, match="VAPID_PRIVATE_KEY"):
            transport.send(format_push(event_for(make_record())), devices=[])
