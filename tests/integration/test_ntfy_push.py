"""Level 4 - the ntfy push transport and one-alert-per-job planning.

Nothing here reaches ntfy.sh: the transport talks to the project's own
:class:`~jobmonitor.http.HttpClient` over a scripted transport, so each test
asserts the exact JSON ntfy would have received and what the notifier does with
each answer. Whether the phone then shows it is the user's one-time check:
``python -m jobmonitor.cli push-test --sample`` (see README).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from tests.conftest import make_company
from tests.support.http import FakeTransport, RecordingSleeper, ScriptedResponse

from jobmonitor.config import EmailSettings, HttpSettings, PushSettings, for_tests
from jobmonitor.http import HttpClient
from jobmonitor.models.job import Job
from jobmonitor.models.record import JobRecord
from jobmonitor.notifications import Channel, NotificationEvent, Notifier, format_push
from jobmonitor.notifications.events import Urgency
from jobmonitor.notifications.transports import (
    DeliveryError,
    MemoryEmailTransport,
    NtfyPushTransport,
    TransportUnavailable,
    build_push_transport,
)
from jobmonitor.storage.memory import InMemoryJobRepository

pytestmark = pytest.mark.integration

T0 = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
TOPIC = "jobs-test-topic"


def record(identifier: str = "1", *, company: str = "Stripe", priority: str = "high") -> JobRecord:
    job = Job(
        company=company,
        title="Software Engineer Intern",
        url=f"https://jobs.example.test/{company.lower()}/{identifier}?src=feed",
        source="greenhouse",
        external_id=identifier,
        location="San Francisco",
    )
    return JobRecord.from_job(
        job,
        relevance_score=90,
        company=make_company(company, priority=priority),  # type: ignore[arg-type]
        now=T0,
    )


def build(
    responses: list[ScriptedResponse],
    *,
    settings: PushSettings | None = None,
    max_attempts: int = 3,
) -> tuple[NtfyPushTransport, FakeTransport]:
    fake = FakeTransport(responses)
    client = HttpClient(
        HttpSettings(max_attempts=max_attempts, backoff_base_seconds=0.0),
        transport=fake,
        sleep=RecordingSleeper(),
    )
    transport = NtfyPushTransport(
        settings or PushSettings(transport="ntfy", ntfy_topic=TOPIC), client=client
    )
    return transport, fake


def single(rec: JobRecord | None = None, urgency: Urgency = Urgency.IMMEDIATE):  # type: ignore[no-untyped-def]
    event = NotificationEvent.for_records(
        [rec or record()], app_base_url="https://app.test", urgency=urgency
    )
    return format_push(event)


class TestRequestShape:
    def test_publishes_json_to_the_server_root_with_the_topic_in_the_body(self) -> None:
        transport, fake = build([ScriptedResponse.json({"id": "abc"})])
        receipts = transport.send(single(), devices=[])

        assert receipts == ["ntfy-abc"]
        assert fake.last_request.method == "POST"
        assert fake.last_request.url == "https://ntfy.sh"
        body = fake.bodies()[0]
        assert body["topic"] == TOPIC
        assert body["title"] == "Stripe - new internship"
        assert body["message"] == "Software Engineer Intern (San Francisco)"

    def test_tapping_opens_the_application_and_the_buttons_open_or_copy_it(self) -> None:
        transport, fake = build([ScriptedResponse.json({"id": "abc"})])
        transport.send(single(), devices=[])

        body = fake.bodies()[0]
        url = "https://jobs.example.test/stripe/1?src=feed"
        assert body["click"] == url
        assert body["actions"] == [
            {"action": "view", "label": "Open application", "url": url},
            {"action": "copy", "label": "Copy link", "value": url},
        ]

    def test_high_priority_companies_ring_louder(self) -> None:
        transport, fake = build([ScriptedResponse.json({}), ScriptedResponse.json({})])
        transport.send(single(urgency=Urgency.IMMEDIATE), devices=[])
        transport.send(single(record(priority="medium"), urgency=Urgency.BATCHED), devices=[])
        assert [body["priority"] for body in fake.bodies()] == [4, 3]

    def test_a_grouped_alert_has_no_copy_button_and_opens_the_feed(self) -> None:
        event = NotificationEvent.for_records(
            [record("1"), record("2")], app_base_url="https://app.test"
        )
        transport, fake = build([ScriptedResponse.json({})])
        transport.send(format_push(event), devices=[])
        body = fake.bodies()[0]
        assert "actions" not in body
        assert body["click"] == "https://app.test/"

    def test_an_access_token_is_sent_as_a_bearer_header(self) -> None:
        settings = PushSettings(transport="ntfy", ntfy_topic=TOPIC, ntfy_token="tk_secret")
        transport, fake = build([ScriptedResponse.json({})], settings=settings)
        transport.send(single(), devices=[])
        assert fake.last_request.headers["Authorization"] == "Bearer tk_secret"

    def test_no_token_means_no_authorization_header(self) -> None:
        transport, fake = build([ScriptedResponse.json({})])
        transport.send(single(), devices=[])
        assert "Authorization" not in fake.last_request.headers

    def test_a_self_hosted_server_is_honoured(self) -> None:
        settings = PushSettings(
            transport="ntfy", ntfy_topic=TOPIC, ntfy_server="https://ntfy.example.org"
        )
        transport, fake = build([ScriptedResponse.json({})], settings=settings)
        transport.send(single(), devices=[])
        assert fake.last_request.url == "https://ntfy.example.org"


class TestFailures:
    def test_no_topic_is_a_configuration_error_not_a_silent_success(self) -> None:
        transport, fake = build([], settings=PushSettings(transport="ntfy"))
        with pytest.raises(TransportUnavailable, match="NTFY_TOPIC"):
            transport.send(single(), devices=[])
        assert fake.requests == []

    def test_a_transient_error_is_retried_then_succeeds(self) -> None:
        transport, fake = build(
            [ScriptedResponse.error(500), ScriptedResponse.json({"id": "later"})]
        )
        assert transport.send(single(), devices=[]) == ["ntfy-later"]
        assert len(fake.requests) == 2

    def test_a_persistent_failure_raises_delivery_error(self) -> None:
        transport, _ = build([ScriptedResponse.error(429)] * 2, max_attempts=2)
        with pytest.raises(DeliveryError, match="ntfy publish failed"):
            transport.send(single(), devices=[])

    def test_the_factory_builds_it(self) -> None:
        transport = build_push_transport(PushSettings(transport="ntfy", ntfy_topic=TOPIC))
        assert isinstance(transport, NtfyPushTransport)


class TestNotifierWithNtfy:
    """ntfy's buttons act on one link, so every job gets its own alert."""

    def notifier(
        self, fake_responses: list[ScriptedResponse]
    ) -> tuple[Notifier, FakeTransport, InMemoryJobRepository, MemoryEmailTransport]:
        transport, fake = build(fake_responses)
        repository = InMemoryJobRepository()
        email = MemoryEmailTransport()
        settings = for_tests(email=EmailSettings(transport="memory", mode="digest"))
        notifier = Notifier(
            settings, repository=repository, email_transport=email, push_transport=transport
        )
        return notifier, fake, repository, email

    def test_each_job_is_its_own_notification_even_at_medium_priority(self) -> None:
        notifier, fake, _repository, email = self.notifier([ScriptedResponse.json({})] * 3)
        records = [record(str(i), company=f"Co{i}", priority="medium") for i in range(3)]
        outcome = notifier.notify(records, now=T0)

        assert len(outcome.events) == 3
        assert all(event.urgency is Urgency.BATCHED for event in outcome.events)
        assert [body["actions"][1]["value"] for body in fake.bodies()] == [r.url for r in records]
        # Digest mode: the poll itself sends no email.
        assert email.sent == []
        assert outcome.delivered_channels == {Channel.PUSH}

    def test_a_failed_publish_leaves_only_that_job_unalerted(self) -> None:
        notifier, _fake, _repository, _email = self.notifier(
            [ScriptedResponse.json({})] + [ScriptedResponse.error(503)] * 3
        )
        records = [record("1", company="A"), record("2", company="B")]
        outcome = notifier.notify(records, now=T0)
        assert outcome.notified_job_ids == [records[0].job_id]
        assert outcome.failed == 1
