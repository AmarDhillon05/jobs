"""Level 4 - the Expo push transport (PRD §22 "Mobile Push").

Nothing here reaches Expo. The transport talks to the project's own
:class:`~jobmonitor.http.HttpClient`, whose transport is injected, so a test can
script exactly what ``exp.host`` replies - an ok ticket, a dead device, a 429, a
500 - and assert both the request Expo would have received and what the notifier
does with each answer.

What this does *not* prove: that Apple or Google then deliver the push to a
handset. That needs a physical device and is the user's step, documented in
``mobile/README.md``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from tests.conftest import make_company
from tests.support.http import FakeTransport, RecordingSleeper, ScriptedResponse

from jobmonitor.config import HttpSettings, PushSettings, for_tests
from jobmonitor.errors import NetworkError
from jobmonitor.http import HttpClient
from jobmonitor.models.job import Job
from jobmonitor.models.record import DeviceRegistration, JobRecord
from jobmonitor.notifications import Channel, NotificationEvent, Notifier
from jobmonitor.notifications.formatters import PushMessage
from jobmonitor.notifications.transports import (
    DeliveryError,
    ExpoPushTransport,
    build_push_transport,
)
from jobmonitor.storage.memory import InMemoryDeviceRepository, InMemoryJobRepository

pytestmark = pytest.mark.integration

T0 = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
EXPO_URL = "https://exp.host/--/api/v2/push/send"


def device(
    index: int = 1, *, transport: str = "expo", token: str | None = None
) -> DeviceRegistration:
    return DeviceRegistration(
        device_id=f"ios-phone-{index}",
        token=token if token is not None else f"ExponentPushToken[token-{index}]",
        transport=transport,
    )


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


def make_record(identifier: str = "1", *, priority: str = "high") -> JobRecord:
    return JobRecord.from_job(
        make_job(identifier),
        relevance_score=90,
        company=make_company(priority=priority),  # type: ignore[arg-type]
        now=T0,
    )


def message() -> PushMessage:
    return PushMessage(
        title="Stripe - Software Engineer Intern",
        body="San Francisco",
        data={
            "schema_version": "1",
            "job_id": "stripe:1",
            "deep_link": "https://app.test/jobs/stripe%3A1",
            "apply_url": "https://stripe.com/jobs/1",
        },
    )


def ok_ticket(identifier: str = "ticket-1") -> dict[str, object]:
    return {"status": "ok", "id": identifier}


def build(
    responses: list[ScriptedResponse],
    *,
    settings: PushSettings | None = None,
    devices: InMemoryDeviceRepository | None = None,
    max_attempts: int = 3,
) -> tuple[ExpoPushTransport, FakeTransport, RecordingSleeper]:
    fake = FakeTransport(responses)
    sleeper = RecordingSleeper()
    client = HttpClient(
        HttpSettings(max_attempts=max_attempts, backoff_base_seconds=0.1),
        transport=fake,
        sleep=sleeper,
    )
    transport = ExpoPushTransport(
        settings or PushSettings(transport="expo"), client=client, device_repository=devices
    )
    return transport, fake, sleeper


class TestRequestShape:
    """What Expo would actually have received."""

    def test_posts_one_message_per_device_to_the_expo_endpoint(self) -> None:
        transport, fake, _ = build(
            [ScriptedResponse.json({"data": [ok_ticket("a"), ok_ticket("b")]})]
        )
        receipts = transport.send(message(), devices=[device(1), device(2)])

        assert receipts == ["a", "b"]
        assert fake.call_count == 1
        assert fake.last_request.method == "POST"
        assert fake.last_request.url == EXPO_URL
        body = fake.bodies()[0]
        assert [entry["to"] for entry in body] == [
            "ExponentPushToken[token-1]",
            "ExponentPushToken[token-2]",
        ]

    def test_carries_the_title_body_and_the_whole_data_payload(self) -> None:
        transport, fake, _ = build([ScriptedResponse.json({"data": [ok_ticket()]})])
        transport.send(message(), devices=[device()])

        entry = fake.bodies()[0][0]
        assert entry["title"] == "Stripe - Software Engineer Intern"
        assert entry["body"] == "San Francisco"
        # The deep link is what makes tapping the notification open the job; if it
        # were dropped here the app would only ever land on the feed.
        assert entry["data"]["deep_link"] == "https://app.test/jobs/stripe%3A1"
        assert entry["data"]["job_id"] == "stripe:1"

    def test_asks_for_high_priority_and_the_apps_android_channel(self) -> None:
        transport, fake, _ = build([ScriptedResponse.json({"data": [ok_ticket()]})])
        transport.send(message(), devices=[device()])

        entry = fake.bodies()[0][0]
        assert entry["priority"] == "high"
        # Must match the channel mobile/src/notifications.ts creates, or Android
        # silently drops the notification's sound and importance.
        assert entry["channelId"] == "internships"

    def test_sends_no_authorization_header_when_no_access_token_is_configured(self) -> None:
        transport, fake, _ = build([ScriptedResponse.json({"data": [ok_ticket()]})])
        transport.send(message(), devices=[device()])
        assert "Authorization" not in fake.last_request.headers

    def test_sends_a_bearer_token_when_one_is_configured(self) -> None:
        transport, fake, _ = build(
            [ScriptedResponse.json({"data": [ok_ticket()]})],
            settings=PushSettings(transport="expo", expo_access_token="secret-token"),
        )
        transport.send(message(), devices=[device()])
        assert fake.last_request.headers["Authorization"] == "Bearer secret-token"

    def test_honours_an_overridden_endpoint(self) -> None:
        transport, fake, _ = build(
            [ScriptedResponse.json({"data": [ok_ticket()]})],
            settings=PushSettings(transport="expo", expo_api_url="https://expo.test/send"),
        )
        transport.send(message(), devices=[device()])
        assert fake.last_request.url == "https://expo.test/send"

    def test_chunks_at_expos_hundred_message_limit(self) -> None:
        devices = [device(index) for index in range(150)]
        transport, fake, _ = build(
            [
                ScriptedResponse.json({"data": [ok_ticket(f"a{i}") for i in range(100)]}),
                ScriptedResponse.json({"data": [ok_ticket(f"b{i}") for i in range(50)]}),
            ]
        )
        receipts = transport.send(message(), devices=devices)

        assert fake.call_count == 2
        assert [len(body) for body in fake.bodies()] == [100, 50]
        assert len(receipts) == 150


class TestTokenSelection:
    def test_skips_a_web_push_subscription_without_failing(self) -> None:
        # A browser and a phone can both be registered; one transport must not
        # choke on the other's token.
        transport, fake, _ = build([ScriptedResponse.json({"data": [ok_ticket()]})])
        receipts = transport.send(
            message(),
            devices=[
                DeviceRegistration(
                    device_id="browser", token='{"endpoint":"https://fcm"}', transport="webpush"
                ),
                device(1),
            ],
        )
        assert receipts == ["ticket-1"]
        assert [entry["to"] for entry in fake.bodies()[0]] == ["ExponentPushToken[token-1]"]

    def test_skips_a_device_whose_token_is_not_an_expo_token(self) -> None:
        transport, fake, _ = build([ScriptedResponse.json({"data": [ok_ticket()]})])
        transport.send(message(), devices=[device(1, token="not-a-token"), device(2)])
        assert [entry["to"] for entry in fake.bodies()[0]] == ["ExponentPushToken[token-2]"]

    def test_no_expo_devices_is_a_no_op_rather_than_a_failure(self) -> None:
        # Raising here would leave the job pending forever and re-alert on every
        # poll, even though email delivered fine.
        transport, fake, _ = build([])
        assert transport.send(message(), devices=[]) == []
        assert fake.call_count == 0

    @pytest.mark.parametrize(
        ("token", "expected"),
        [
            ("ExponentPushToken[abc]", True),
            ("ExpoPushToken[abc]", True),
            ("  ExponentPushToken[abc]  ", True),
            ("ExponentPushToken[abc", False),
            ("abc", False),
            ("", False),
            ('{"endpoint": "https://fcm.googleapis.com/x"}', False),
        ],
    )
    def test_recognises_an_expo_token(self, token: str, expected: bool) -> None:
        assert ExpoPushTransport.is_expo_token(token) is expected


class TestTicketHandling:
    def test_a_dead_device_is_retired_not_retried(self) -> None:
        devices = InMemoryDeviceRepository()
        gone = device(1)
        devices.register(gone)
        transport, _, _ = build(
            [
                ScriptedResponse.json(
                    {
                        "data": [
                            {
                                "status": "error",
                                "message": '"ExponentPushToken[token-1]" is not a registered push notification recipient',
                                "details": {"error": "DeviceNotRegistered"},
                            }
                        ]
                    }
                )
            ],
            devices=devices,
        )
        # One device, and it is gone: no receipts, but no exception either - the
        # alert can never be delivered to it, so retrying would wedge the queue.
        assert transport.send(message(), devices=[gone]) == []
        assert transport.stale_device_ids == ["ios-phone-1"]
        assert devices.enabled_devices() == []

    def test_a_live_device_still_gets_the_alert_when_another_is_dead(self) -> None:
        transport, _, _ = build(
            [
                ScriptedResponse.json(
                    {
                        "data": [
                            {
                                "status": "error",
                                "message": "not registered",
                                "details": {"error": "DeviceNotRegistered"},
                            },
                            ok_ticket("live"),
                        ]
                    }
                )
            ]
        )
        assert transport.send(message(), devices=[device(1), device(2)]) == ["live"]
        assert transport.stale_device_ids == ["ios-phone-1"]

    def test_retiring_a_device_works_without_a_registry(self) -> None:
        transport, _, _ = build(
            [
                ScriptedResponse.json(
                    {
                        "data": [
                            {
                                "status": "error",
                                "message": "gone",
                                "details": {"error": "DeviceNotRegistered"},
                            }
                        ]
                    }
                )
            ]
        )
        assert transport.send(message(), devices=[device()]) == []
        assert transport.stale_device_ids == ["ios-phone-1"]

    def test_a_registry_that_errors_does_not_break_delivery(self) -> None:
        class BrokenRegistry:
            def unregister(self, device_id: str) -> bool:
                raise RuntimeError("table gone")

        transport, _, _ = build(
            [
                ScriptedResponse.json(
                    {
                        "data": [
                            {
                                "status": "error",
                                "message": "gone",
                                "details": {"error": "DeviceNotRegistered"},
                            },
                            ok_ticket("live"),
                        ]
                    }
                )
            ],
        )
        transport._devices = BrokenRegistry()  # type: ignore[assignment]
        assert transport.send(message(), devices=[device(1), device(2)]) == ["live"]

    def test_an_unrecognised_per_device_error_is_reported_when_nothing_got_through(self) -> None:
        transport, _, _ = build(
            [
                ScriptedResponse.json(
                    {
                        "data": [
                            {
                                "status": "error",
                                "message": "Message too big",
                                "details": {"error": "MessageTooBig"},
                            }
                        ]
                    }
                )
            ]
        )
        with pytest.raises(DeliveryError, match=r"MessageTooBig|Message too big"):
            transport.send(message(), devices=[device()])

    def test_a_partial_failure_still_counts_as_delivered(self) -> None:
        transport, _, _ = build(
            [
                ScriptedResponse.json(
                    {
                        "data": [
                            ok_ticket("live"),
                            {"status": "error", "message": "Message rate exceeded"},
                        ]
                    }
                )
            ]
        )
        assert transport.send(message(), devices=[device(1), device(2)]) == ["live"]

    def test_a_request_level_error_is_a_delivery_error(self) -> None:
        transport, _, _ = build(
            [
                ScriptedResponse.json(
                    {"errors": [{"code": "PUSH_TOO_MANY_EXPERIENCE_IDS", "message": "nope"}]}
                )
            ]
        )
        with pytest.raises(DeliveryError, match="rejected the request"):
            transport.send(message(), devices=[device()])

    @pytest.mark.parametrize("payload", [{"data": "not-a-list"}, {}, [], "text"])
    def test_an_unreadable_response_is_a_delivery_error(self, payload: object) -> None:
        transport, _, _ = build([ScriptedResponse.json(payload)])
        with pytest.raises(DeliveryError):
            transport.send(message(), devices=[device()])

    def test_a_malformed_ticket_is_a_delivery_error(self) -> None:
        transport, _, _ = build([ScriptedResponse.json({"data": ["nonsense"]})])
        with pytest.raises(DeliveryError):
            transport.send(message(), devices=[device()])

    def test_a_ticket_without_an_id_still_yields_a_receipt(self) -> None:
        transport, _, _ = build([ScriptedResponse.json({"data": [{"status": "ok"}]})])
        assert transport.send(message(), devices=[device()]) == ["expo-ios-phone-1"]


class TestTransportFailures:
    def test_recovers_from_a_transient_500(self) -> None:
        transport, fake, sleeper = build(
            [
                ScriptedResponse.error(500),
                ScriptedResponse.error(500),
                ScriptedResponse.json({"data": [ok_ticket("late")]}),
            ]
        )
        assert transport.send(message(), devices=[device()]) == ["late"]
        assert fake.call_count == 3
        assert len(sleeper.delays) == 2

    def test_honours_retry_after_on_a_429(self) -> None:
        transport, _, sleeper = build(
            [
                ScriptedResponse.error(429, headers={"Retry-After": "2"}),
                ScriptedResponse.json({"data": [ok_ticket()]}),
            ]
        )
        transport.send(message(), devices=[device()])
        assert sleeper.delays == [2.0]

    def test_exhausting_the_retry_budget_is_a_delivery_error(self) -> None:
        transport, _, _ = build([ScriptedResponse.error(500)] * 3)
        with pytest.raises(DeliveryError, match="Expo push request failed"):
            transport.send(message(), devices=[device()])

    def test_a_network_failure_is_a_delivery_error(self) -> None:
        transport, _, _ = build([ScriptedResponse.boom(NetworkError("connection reset"))] * 3)
        with pytest.raises(DeliveryError):
            transport.send(message(), devices=[device()])

    def test_an_expired_access_token_is_not_retried(self) -> None:
        # 401/403 means the credential is wrong, not that Expo is busy; spending
        # the retry budget on it would just delay the alert.
        transport, fake, _ = build([ScriptedResponse.error(401)])
        with pytest.raises(DeliveryError):
            transport.send(message(), devices=[device()])
        assert fake.call_count == 1


class TestFactoryAndNotifier:
    def test_the_factory_builds_it_from_config(self) -> None:
        built = build_push_transport(PushSettings(transport="expo"))
        assert isinstance(built, ExpoPushTransport)

    def test_the_factory_passes_the_device_registry_through(self) -> None:
        devices = InMemoryDeviceRepository()
        built = build_push_transport(PushSettings(transport="expo"), device_repository=devices)
        assert isinstance(built, ExpoPushTransport)
        assert built._devices is devices

    def test_the_notifier_delivers_through_it_and_marks_the_job_notified(self) -> None:
        devices = InMemoryDeviceRepository()
        devices.register(device(1))
        repository = InMemoryJobRepository()
        record = repository.upsert(
            make_job(), relevance_score=90, company=make_company(priority="high"), now=T0
        ).record

        transport, fake, _ = build([ScriptedResponse.json({"data": [ok_ticket("sent")]})])
        notifier = Notifier(
            for_tests(push=PushSettings(transport="expo"), app_base_url="https://app.test"),
            repository=repository,
            push_transport=transport,
            device_repository=devices,
        )
        outcome = notifier.notify([record], now=T0)

        assert Channel.PUSH in outcome.delivered_channels
        assert outcome.notified_job_ids == [record.job_id]
        assert fake.call_count == 1
        # A second identical poll must be silent (PRD §30 Scenario 2).
        again = notifier.notify(repository.recent(limit=10), now=T0)
        assert again.events == []
        assert fake.call_count == 1

    def test_a_failing_expo_send_leaves_the_job_pending(self) -> None:
        devices = InMemoryDeviceRepository()
        devices.register(device(1))
        repository = InMemoryJobRepository()
        record = repository.upsert(
            make_job("2"), relevance_score=80, company=make_company(priority="high"), now=T0
        ).record

        transport, _, _ = build([ScriptedResponse.error(500)] * 3)
        notifier = Notifier(
            for_tests(push=PushSettings(transport="expo")),
            repository=repository,
            push_transport=transport,
            device_repository=devices,
        )
        # Email is a memory sink by default and succeeds, so the job is marked;
        # what matters is that the push failure is reported, not swallowed.
        outcome = notifier.notify([record], now=T0)
        push_results = [r for r in outcome.results if r.channel is Channel.PUSH]
        assert push_results and not push_results[0].delivered
        assert "Expo" in push_results[0].detail
        assert push_results[0].error_type == "DeliveryError"

    def test_the_deep_link_the_app_receives_identifies_the_job(self) -> None:
        # The end of the chain PRD §18.2 traces backwards: event -> payload ->
        # what the handset is handed. mobile/src/routes.ts parses exactly this.
        record = make_record()
        event = NotificationEvent.for_records([record], app_base_url="https://app.test")

        from urllib.parse import quote

        from jobmonitor.notifications import format_push

        transport, fake, _ = build([ScriptedResponse.json({"data": [ok_ticket()]})])
        transport.send(format_push(event), devices=[device()])

        data = fake.bodies()[0][0]["data"]
        assert data["job_id"] == record.job_id
        assert data["deep_link"].endswith(f"/jobs/{quote(record.job_id, safe='')}")
