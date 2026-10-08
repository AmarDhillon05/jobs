"""Delivery transports (PRD §22: "push-provider calls must be abstracted so tests
can use a fake transport").

Email
-----
``memory`` (tests), ``console`` (local runs) and ``ses`` (deployed).

Push
----
``memory``, ``console``, ``sns``, ``expo``, ``webpush`` and ``ntfy``.

``ntfy`` is the recommended personal path: the phone runs the free ntfy app
subscribed to a private topic, and each new job arrives as its own notification
with "Open application" and "Copy link" buttons. It needs no app of ours, no
signing keys and no dependency beyond the project's HTTP client.

``expo`` is the path to the React Native app in ``mobile/``: it POSTs to Expo's
public push API over the project's own retrying HTTP client, so it needs nothing
beyond urllib3 - already in the Lambda runtime.

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

import logging
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from jobmonitor.config import EmailSettings, PushSettings
from jobmonitor.errors import JobMonitorError
from jobmonitor.models.record import DeviceRegistration
from jobmonitor.notifications.formatters import EmailMessage, PushMessage

logger = logging.getLogger(__name__)


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
    #: True when each job should arrive as its own notification (so its buttons
    #: can act on that job's link) rather than grouped per poll.
    one_alert_per_job: bool = False

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


class ExpoPushTransport(PushTransport):
    """Expo's push service - the path to the React Native app in ``mobile/``.

    One HTTPS POST carrying up to 100 messages; Expo relays each to APNs or FCM.
    Chosen over talking to APNs/FCM directly because it needs no signing key, no
    extra dependency (the project's own :class:`~jobmonitor.http.HttpClient` over
    urllib3 is enough) and no per-platform code - see ARCHITECTURE.md §6.

    Two failure modes need separating, because they want opposite handling:

    * a **transport/HTTP** failure (Expo down, 429, network) is retryable, so it
      raises :class:`DeliveryError` and the queue redelivers the alert;
    * a **per-ticket** ``DeviceNotRegistered`` means that handset uninstalled the
      app or reinstalled with a new token. Retrying can never succeed, so the
      device is disabled in the registry (when one was supplied) and the send
      counts as delivered for the remaining devices. Raising here instead would
      wedge every future alert behind one dead phone.
    """

    name = "expo"
    #: Expo rejects a request carrying more than this many messages.
    BATCH_SIZE = 100

    def __init__(
        self,
        settings: PushSettings,
        *,
        client: Any = None,
        device_repository: Any = None,
    ) -> None:
        self.settings = settings
        self._client = client
        self._devices = device_repository
        #: device_ids Expo reported as gone, in order. Read by tests and logs.
        self.stale_device_ids: list[str] = []

    def _get_client(self) -> Any:
        if self._client is None:
            from jobmonitor.http import HttpClient

            self._client = HttpClient()
        return self._client

    @staticmethod
    def is_expo_token(token: str) -> bool:
        text = token.strip()
        return text.startswith(("ExponentPushToken[", "ExpoPushToken[")) and text.endswith("]")

    def _messages(
        self, message: PushMessage, devices: Sequence[DeviceRegistration]
    ) -> list[dict[str, Any]]:
        return [
            {
                "to": device.token.strip(),
                "title": message.title,
                "body": message.body,
                "data": dict(message.data),
                "sound": "default",
                # "high" wakes the device promptly; a new internship is the whole
                # point of the app, and the volume is a handful a day.
                "priority": "high",
                # Matches the channel mobile/src/notifications.ts creates.
                "channelId": "internships",
            }
            for device in devices
        ]

    def send(self, message: PushMessage, *, devices: Sequence[DeviceRegistration]) -> list[str]:
        targets = [
            device
            for device in devices
            if device.transport in ("expo", "") and self.is_expo_token(device.token)
        ]
        skipped = len(devices) - len(targets)
        if skipped:
            # A mixed registry (a browser's Web Push subscription alongside a
            # phone's Expo token) must not make this transport fail.
            logger.info("expo push: skipped %d device(s) with a non-Expo token", skipped)
        if not targets:
            # Nothing to do is not a failure: the email channel still delivered,
            # and treating it as one would leave the job pending forever.
            return []

        headers = {"Accept-Encoding": "gzip, deflate", "Content-Type": "application/json"}
        if self.settings.expo_access_token:
            headers["Authorization"] = f"Bearer {self.settings.expo_access_token}"

        receipts: list[str] = []
        client = self._get_client()
        for start in range(0, len(targets), self.BATCH_SIZE):
            batch = targets[start : start + self.BATCH_SIZE]
            try:
                payload = client.post_json(
                    self.settings.expo_api_url, self._messages(message, batch), headers=headers
                )
            except JobMonitorError as exc:
                raise DeliveryError(f"Expo push request failed: {exc}") from exc
            receipts.extend(self._read_tickets(payload, batch))
        return receipts

    def _read_tickets(self, payload: Any, batch: Sequence[DeviceRegistration]) -> list[str]:
        if not isinstance(payload, dict):
            raise DeliveryError(f"Expo push: unexpected response {type(payload).__name__}")
        if payload.get("errors"):
            # A request-level error: malformed body, bad access token, ...
            raise DeliveryError(f"Expo push rejected the request: {payload['errors']}")
        tickets = payload.get("data")
        if not isinstance(tickets, list):
            raise DeliveryError("Expo push: response had no 'data' array of tickets")

        receipts: list[str] = []
        retryable: list[str] = []
        for device, ticket in zip(batch, tickets, strict=False):
            if not isinstance(ticket, dict):
                retryable.append(f"{device.device_id}: malformed ticket {ticket!r}")
                continue
            if ticket.get("status") == "ok":
                receipts.append(str(ticket.get("id") or f"expo-{device.device_id}"))
                continue
            detail = str(ticket.get("message") or "unknown error")
            error_code = str((ticket.get("details") or {}).get("error") or "")
            if error_code == "DeviceNotRegistered":
                self._retire(device, detail)
                continue
            retryable.append(f"{device.device_id}: {detail}")

        if retryable and not receipts:
            # Every device in this batch failed for a reason that might clear:
            # surface it so the notifier leaves the job pending and the queue
            # retries, rather than silently reporting success.
            raise DeliveryError("Expo push failed for every device: " + "; ".join(retryable))
        for problem in retryable:
            logger.warning("expo push: %s", problem)
        return receipts

    def _retire(self, device: DeviceRegistration, detail: str) -> None:
        self.stale_device_ids.append(device.device_id)
        logger.warning(
            "expo push: device %s is no longer registered (%s); disabling it",
            device.device_id,
            detail,
        )
        if self._devices is None:
            return
        try:
            self._devices.unregister(device.device_id)
        except Exception:  # pragma: no cover - never let cleanup break delivery
            logger.exception("could not unregister stale device %s", device.device_id)


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


class NtfyPushTransport(PushTransport):
    """Publish to an ntfy topic (https://docs.ntfy.sh/publish/).

    One JSON POST to the server root per alert. A single-job alert carries:

    * ``click`` - tapping the notification opens the application page;
    * a ``view`` button, "Open application";
    * a ``copy`` button, "Copy link", which puts the application URL on the
      clipboard (supported by ntfy's Android and web apps; iOS shows the other
      button and the tap target only).

    Device registrations are not used: whoever subscribes to the topic receives
    it. A failed publish raises :class:`DeliveryError`, leaving the job pending.
    """

    name = "ntfy"
    one_alert_per_job = True
    #: ntfy priorities: 3 is default, 4 is "high" (sound + heads-up on Android).
    PRIORITY_IMMEDIATE = 4
    PRIORITY_DEFAULT = 3

    def __init__(self, settings: PushSettings, *, client: Any = None) -> None:
        self.settings = settings
        self._client = client

    def _get_client(self) -> Any:
        if self._client is None:
            from jobmonitor.http import HttpClient

            self._client = HttpClient()
        return self._client

    #: ntfy tags (shown as emoji) by kind of posting.
    TAGS = {"event": "date", "industry_event": "date", "program": "mortar_board"}
    #: The "open" button's label by kind of posting.
    OPEN_LABELS = {"event": "Open event", "industry_event": "Open event", "program": "Open program"}

    def payload(self, message: PushMessage) -> dict[str, Any]:
        apply_url = message.data.get("apply_url", "")
        kind = message.data.get("kind", "internship")
        body: dict[str, Any] = {
            "topic": self.settings.ntfy_topic,
            "title": message.title,
            "message": message.body,
            "tags": [self.TAGS.get(kind, "briefcase")],
            "priority": self.PRIORITY_IMMEDIATE
            if message.data.get("urgency") == "immediate"
            else self.PRIORITY_DEFAULT,
        }
        kit_url = message.data.get("kit_url", "")
        if apply_url and kit_url:
            # Tapping opens the Apply kit: the answers, in the form's order, with
            # an "Open application" button at the top. ntfy allows three actions.
            body["click"] = kit_url
            body["actions"] = [
                {"action": "view", "label": "Apply kit", "url": kit_url},
                {"action": "view", "label": "Open application", "url": apply_url},
                {"action": "copy", "label": "Copy link", "value": apply_url},
            ]
        elif apply_url:
            body["click"] = apply_url
            body["actions"] = [
                {
                    "action": "view",
                    "label": self.OPEN_LABELS.get(kind, "Open application"),
                    "url": apply_url,
                },
                {"action": "copy", "label": "Copy link", "value": apply_url},
            ]
        elif message.deep_link:
            body["click"] = message.deep_link
        return body

    def send(self, message: PushMessage, *, devices: Sequence[DeviceRegistration]) -> list[str]:
        if not self.settings.ntfy_topic:
            raise TransportUnavailable("NtfyPushTransport needs NTFY_TOPIC to be configured")
        headers = {"Content-Type": "application/json"}
        if self.settings.ntfy_token:
            headers["Authorization"] = f"Bearer {self.settings.ntfy_token}"
        try:
            response = self._get_client().post_json(
                self.settings.ntfy_server, self.payload(message), headers=headers
            )
        except JobMonitorError as exc:
            raise DeliveryError(f"ntfy publish failed: {exc}") from exc
        receipt = response.get("id") if isinstance(response, dict) else None
        return [f"ntfy-{receipt or 'sent'}"]


# ------------------------------------------------------------------- factories

EMAIL_TRANSPORTS = ("memory", "console", "ses")
PUSH_TRANSPORTS = ("memory", "console", "sns", "expo", "webpush", "ntfy")


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
    device_repository: Any = None,
) -> PushTransport:
    choice = settings.transport.lower()
    if choice == "memory":
        return MemoryPushTransport()
    if choice == "console":
        return ConsolePushTransport()
    if choice == "sns":
        return SnsPushTransport(topic_arn, region=region, endpoint_url=endpoint_url)
    if choice == "expo":
        return ExpoPushTransport(settings, device_repository=device_repository)
    if choice == "webpush":
        return WebPushTransport(settings)
    if choice == "ntfy":
        return NtfyPushTransport(settings)
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
    "ExpoPushTransport",
    "MemoryEmailTransport",
    "MemoryPushTransport",
    "NtfyPushTransport",
    "PushTransport",
    "SesEmailTransport",
    "SnsPushTransport",
    "TransportUnavailable",
    "WebPushTransport",
    "build_email_transport",
    "build_push_transport",
)
