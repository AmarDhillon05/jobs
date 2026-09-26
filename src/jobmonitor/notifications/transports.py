"""Delivery transports (PRD §22: "push-provider calls must be abstracted so tests
can use a fake transport").

Email
-----
``memory`` (tests), ``console`` (local runs) and ``ses`` (deployed).

Push
----
``memory``, ``console``, ``sns`` and ``webpush``.

``sns`` is the default deployable push path and needs **no dependency beyond
boto3**, which the Lambda runtime already has - so the whole push pipeline is
exercisable under Moto and LocalStack with nothing extra installed. ``webpush``
delivers to the installed PWA directly and needs VAPID signing, which needs
``cryptography``; that is an optional extra (``pip install '.[push]'``) plus a
Lambda layer, imported lazily and with an explicit error if absent, so the
zero-dependency deployment story stays intact for everyone who does not use it.
See ARCHITECTURE.md for why Web Push was chosen over Expo/FCM.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from jobmonitor.config import EmailSettings, PushSettings
from jobmonitor.errors import JobMonitorError
from jobmonitor.models.record import DeviceRegistration
from jobmonitor.notifications.formatters import EmailMessage, PushMessage


class DeliveryError(JobMonitorError):
    """A transport could not deliver. Retryable unless stated otherwise."""


class TransportUnavailable(DeliveryError):
    """The transport is not usable in this environment (e.g. a missing extra)."""


# ------------------------------------------------------------------------ email


class EmailTransport(ABC):
    name: str = "email"

    @abstractmethod
    def send(self, message: EmailMessage, *, to: str, sender: str) -> str:
        """Deliver, returning a provider message id. Raise on failure."""


@dataclass
class MemoryEmailTransport(EmailTransport):
    """Captures messages instead of sending. The local sink PRD §22 asks for."""

    name: str = "memory"
    sent: list[tuple[EmailMessage, str, str]] = field(default_factory=list)
    #: Set to raise on the Nth send (1-based), to exercise failure handling.
    fail_on_call: int | None = None
    calls: int = 0

    def send(self, message: EmailMessage, *, to: str, sender: str) -> str:
        self.calls += 1
        if self.fail_on_call is not None and self.calls == self.fail_on_call:
            raise DeliveryError(f"{self.name}: simulated failure on call {self.calls}")
        self.sent.append((message, to, sender))
        return f"memory-email-{len(self.sent)}"

    @property
    def subjects(self) -> list[str]:
        return [message.subject for message, _to, _sender in self.sent]

    def clear(self) -> None:
        self.sent.clear()
        self.calls = 0


class ConsoleEmailTransport(EmailTransport):
    """Prints the email. Used by `make local` so a poll is observable."""

    name = "console"

    def send(self, message: EmailMessage, *, to: str, sender: str) -> str:
        print(f"\n=== EMAIL to {to} (from {sender}) ===")
        print(f"Subject: {message.subject}\n")
        print(message.text_body)
        print("=" * 56)
        return "console-email"


class SesEmailTransport(EmailTransport):
    """Amazon SES. Verified identities are the user's setup step (see README)."""

    name = "ses"

    def __init__(
        self,
        settings: EmailSettings,
        *,
        client: Any = None,
        region: str = "us-east-1",
        endpoint_url: str | None = None,
    ) -> None:
        self.settings = settings
        self._client = client
        self._region = region
        self._endpoint_url = endpoint_url

    def _get_client(self) -> Any:
        if self._client is None:
            import boto3

            kwargs: dict[str, Any] = {"region_name": self._region}
            if self._endpoint_url:
                kwargs["endpoint_url"] = self._endpoint_url
            self._client = boto3.client("ses", **kwargs)
        return self._client

    def send(self, message: EmailMessage, *, to: str, sender: str) -> str:
        from botocore.exceptions import BotoCoreError, ClientError

        request: dict[str, Any] = {
            "Source": sender,
            "Destination": {"ToAddresses": [to]},
            "Message": {
                "Subject": {"Data": message.subject, "Charset": "UTF-8"},
                "Body": {
                    "Text": {"Data": message.text_body, "Charset": "UTF-8"},
                    "Html": {"Data": message.html_body, "Charset": "UTF-8"},
                },
            },
        }
        if self.settings.configuration_set:
            request["ConfigurationSetName"] = self.settings.configuration_set
        try:
            response = self._get_client().send_email(**request)
        except (ClientError, BotoCoreError) as exc:
            raise DeliveryError(f"SES send_email failed: {exc}") from exc
        return str(response.get("MessageId", ""))


# ------------------------------------------------------------------------- push


class PushTransport(ABC):
    name: str = "push"

    @abstractmethod
    def send(self, message: PushMessage, *, devices: Sequence[DeviceRegistration]) -> list[str]:
        """Deliver to each device, returning per-device receipts. Raise on failure."""


@dataclass
class MemoryPushTransport(PushTransport):
    name: str = "memory"
    sent: list[tuple[PushMessage, tuple[str, ...]]] = field(default_factory=list)
    fail_on_call: int | None = None
    calls: int = 0

    def send(self, message: PushMessage, *, devices: Sequence[DeviceRegistration]) -> list[str]:
        self.calls += 1
        if self.fail_on_call is not None and self.calls == self.fail_on_call:
            raise DeliveryError(f"{self.name}: simulated failure on call {self.calls}")
        self.sent.append((message, tuple(device.device_id for device in devices)))
        return [f"memory-push-{device.device_id}" for device in devices]

    @property
    def deep_links(self) -> list[str]:
        return [message.deep_link for message, _devices in self.sent]

    @property
    def titles(self) -> list[str]:
        return [message.title for message, _devices in self.sent]

    def clear(self) -> None:
        self.sent.clear()
        self.calls = 0


class ConsolePushTransport(PushTransport):
    name = "console"

    def send(self, message: PushMessage, *, devices: Sequence[DeviceRegistration]) -> list[str]:
        targets = ", ".join(device.device_id for device in devices) or "<no devices>"
        print(f"\n=== PUSH to {targets} ===")
        print(f"{message.title}\n{message.body}")
        print(f"deep_link: {message.deep_link}")
        print("=" * 56)
        return [f"console-push-{device.device_id}" for device in devices]


class SnsPushTransport(PushTransport):
    """Publish the alert to SNS.

    The default deployable push path: no dependency beyond boto3, so it runs
    under Moto and LocalStack unchanged. The topic can fan out to SNS mobile push
    (APNS/FCM) platform endpoints, SMS, or email - whichever the user subscribes.
    """

    name = "sns"

    def __init__(
        self,
        topic_arn: str | None,
        *,
        client: Any = None,
        region: str = "us-east-1",
        endpoint_url: str | None = None,
    ) -> None:
        self.topic_arn = topic_arn
        self._client = client
        self._region = region
        self._endpoint_url = endpoint_url

    def _get_client(self) -> Any:
        if self._client is None:
            import boto3

            kwargs: dict[str, Any] = {"region_name": self._region}
            if self._endpoint_url:
                kwargs["endpoint_url"] = self._endpoint_url
            self._client = boto3.client("sns", **kwargs)
        return self._client

    def send(self, message: PushMessage, *, devices: Sequence[DeviceRegistration]) -> list[str]:
        from botocore.exceptions import BotoCoreError, ClientError

        if not self.topic_arn:
            raise TransportUnavailable(
                "SnsPushTransport needs NOTIFICATION_TOPIC_ARN to be configured"
            )
        try:
            response = self._get_client().publish(
                TopicArn=self.topic_arn,
                Subject=message.title[:99],  # SNS caps Subject at 100 chars
                Message=message.body,
                MessageAttributes={
                    key: {"DataType": "String", "StringValue": value}
                    for key, value in message.data.items()
                    if value
                },
            )
        except (ClientError, BotoCoreError) as exc:
            raise DeliveryError(f"SNS publish failed: {exc}") from exc
        return [str(response.get("MessageId", ""))]


class WebPushTransport(PushTransport):
    """Web Push (VAPID) straight to the installed PWA.

    Needs ``cryptography`` for ES256 VAPID signing, which is *not* in the Lambda
    runtime - hence the lazy import and the explicit, actionable error. Install
    with ``pip install '.[push]'`` and attach a Lambda layer; see README
    "Remaining manual configuration".
    """

    name = "webpush"

    def __init__(self, settings: PushSettings, *, sender: Any = None) -> None:
        self.settings = settings
        self._sender = sender

    def _get_sender(self) -> Any:
        if self._sender is not None:
            return self._sender
        try:
            from pywebpush import webpush  # type: ignore[import-not-found]
        except ImportError as exc:
            raise TransportUnavailable(
                "WebPushTransport requires the optional 'push' extra: "
                "pip install '.[push]' (and a Lambda layer when deployed). "
                "Use PUSH_TRANSPORT=sns for a dependency-free path."
            ) from exc
        return webpush

    def send(self, message: PushMessage, *, devices: Sequence[DeviceRegistration]) -> list[str]:
        import json

        if not self.settings.vapid_private_key:
            raise TransportUnavailable("WebPushTransport requires VAPID_PRIVATE_KEY")
        sender = self._get_sender()
        payload = json.dumps({"title": message.title, "body": message.body, "data": message.data})
        receipts: list[str] = []
        for device in devices:
            try:
                subscription = json.loads(device.token)
            except ValueError as exc:
                raise DeliveryError(
                    f"device {device.device_id}: token is not a Web Push subscription JSON"
                ) from exc
            try:
                sender(
                    subscription_info=subscription,
                    data=payload,
                    vapid_private_key=self.settings.vapid_private_key,
                    vapid_claims={"sub": self.settings.vapid_subject},
                )
            except Exception as exc:
                raise DeliveryError(f"web push to {device.device_id} failed: {exc}") from exc
            receipts.append(f"webpush-{device.device_id}")
        return receipts


# ------------------------------------------------------------------- factories

EMAIL_TRANSPORTS = ("memory", "console", "ses")
PUSH_TRANSPORTS = ("memory", "console", "sns", "webpush")


def build_email_transport(
    settings: EmailSettings, *, region: str = "us-east-1", endpoint_url: str | None = None
) -> EmailTransport:
    choice = settings.transport.lower()
    if choice == "memory":
        return MemoryEmailTransport()
    if choice == "console":
        return ConsoleEmailTransport()
    if choice == "ses":
        return SesEmailTransport(settings, region=region, endpoint_url=endpoint_url)
    raise TransportUnavailable(
        f"unknown EMAIL_TRANSPORT {settings.transport!r}; expected one of {EMAIL_TRANSPORTS}"
    )


def build_push_transport(
    settings: PushSettings,
    *,
    topic_arn: str | None = None,
    region: str = "us-east-1",
    endpoint_url: str | None = None,
) -> PushTransport:
    choice = settings.transport.lower()
    if choice == "memory":
        return MemoryPushTransport()
    if choice == "console":
        return ConsolePushTransport()
    if choice == "sns":
        return SnsPushTransport(topic_arn, region=region, endpoint_url=endpoint_url)
    if choice == "webpush":
        return WebPushTransport(settings)
    raise TransportUnavailable(
        f"unknown PUSH_TRANSPORT {settings.transport!r}; expected one of {PUSH_TRANSPORTS}"
    )


__all__: Sequence[str] = (
    "EMAIL_TRANSPORTS",
    "PUSH_TRANSPORTS",
    "ConsoleEmailTransport",
    "ConsolePushTransport",
    "DeliveryError",
    "EmailTransport",
    "MemoryEmailTransport",
    "MemoryPushTransport",
    "PushTransport",
    "SesEmailTransport",
    "SnsPushTransport",
    "TransportUnavailable",
    "WebPushTransport",
    "build_email_transport",
    "build_push_transport",
)
