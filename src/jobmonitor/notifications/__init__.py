"""Notification pipeline (PRD §22).

Email is required in the production design; push is delivered through Expo (to
the React Native app in ``mobile/``), SNS, or Web Push straight to the installed
PWA. Every transport is behind an interface with an in-memory fake, so the whole
path is tested end to end without sending anything to anyone.
"""

from jobmonitor.notifications.events import (
    SCHEMA_VERSION,
    Channel,
    DeliveryResult,
    JobAlert,
    NotificationEvent,
    Urgency,
)
from jobmonitor.notifications.formatters import (
    EmailMessage,
    PushMessage,
    format_email,
    format_push,
    summarize,
)
from jobmonitor.notifications.notifier import NotificationOutcome, Notifier
from jobmonitor.notifications.transports import (
    ConsoleEmailTransport,
    ConsolePushTransport,
    DeliveryError,
    EmailTransport,
    ExpoPushTransport,
    MemoryEmailTransport,
    MemoryPushTransport,
    PushTransport,
    SesEmailTransport,
    SnsPushTransport,
    TransportUnavailable,
    WebPushTransport,
    build_email_transport,
    build_push_transport,
)

__all__ = [
    "SCHEMA_VERSION",
    "Channel",
    "ConsoleEmailTransport",
    "ConsolePushTransport",
    "DeliveryError",
    "DeliveryResult",
    "EmailMessage",
    "EmailTransport",
    "ExpoPushTransport",
    "JobAlert",
    "MemoryEmailTransport",
    "MemoryPushTransport",
    "NotificationEvent",
    "NotificationOutcome",
    "Notifier",
    "PushMessage",
    "PushTransport",
    "SesEmailTransport",
    "SnsPushTransport",
    "TransportUnavailable",
    "Urgency",
    "WebPushTransport",
    "build_email_transport",
    "build_push_transport",
    "format_email",
    "format_push",
    "summarize",
]
