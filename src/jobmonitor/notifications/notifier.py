"""Turning new job records into delivered alerts.

Responsibilities, in order of importance:

1. **Never alert twice about the same job.** The repository is the authority:
   records are flagged only after a channel actually succeeded, and
   ``mark_notified`` is conditional, so a redelivered queue message is a no-op.
2. **Never let one channel's failure suppress the other.** Email and push are
   attempted independently; a single success is enough to consider the job
   delivered, and a total failure leaves the record pending so the next poll (or
   a queue retry) tries again.
3. **Respect urgency** (PRD §22): high-priority companies get an alert of their
   own immediately; everything else found in the same poll is grouped into one.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from jobmonitor.config import Settings
from jobmonitor.models.record import DeviceRegistration, JobRecord
from jobmonitor.notifications.events import (
    Channel,
    DeliveryResult,
    NotificationEvent,
    Urgency,
)
from jobmonitor.notifications.formatters import format_email, format_push, summarize
from jobmonitor.notifications.transports import (
    DeliveryError,
    EmailTransport,
    MemoryEmailTransport,
    MemoryPushTransport,
    PushTransport,
    TransportUnavailable,
)
from jobmonitor.storage.base import DeviceRepository, JobRepository

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class NotificationOutcome:
    events: list[NotificationEvent] = field(default_factory=list)
    results: list[DeliveryResult] = field(default_factory=list)
    notified_job_ids: list[str] = field(default_factory=list)
    skipped_job_ids: list[str] = field(default_factory=list)

    @property
    def emitted(self) -> int:
        return sum(1 for result in self.results if result.delivered)

    @property
    def failed(self) -> int:
        return sum(1 for result in self.results if not result.delivered)

    @property
    def delivered_channels(self) -> set[Channel]:
        return {result.channel for result in self.results if result.delivered}

    def to_dict(self) -> dict[str, object]:
        return {
            "events": len(self.events),
            "notifications_emitted": self.emitted,
            "notifications_failed": self.failed,
            "notified_job_ids": list(self.notified_job_ids),
            "skipped_job_ids": list(self.skipped_job_ids),
        }


class Notifier:
    def __init__(
        self,
        settings: Settings,
        *,
        repository: JobRepository,
        email_transport: EmailTransport | None = None,
        push_transport: PushTransport | None = None,
        device_repository: DeviceRepository | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.email = email_transport or MemoryEmailTransport()
        self.push = push_transport or MemoryPushTransport()
        self.devices = device_repository

    # ----------------------------------------------------------------- planning
    def plan(self, records: Sequence[JobRecord]) -> list[NotificationEvent]:
        """Group records into the events we intend to send.

        High-priority companies get one event each (immediate); everything else is
        collapsed into a single grouped event for the poll.
        """
        immediate_priorities = set(self.settings.filters.immediate_alert_priorities)
        events: list[NotificationEvent] = []
        batched: list[JobRecord] = []

        for record in records:
            if record.priority in immediate_priorities:
                events.append(
                    NotificationEvent.for_records(
                        [record],
                        app_base_url=self.settings.app_base_url,
                        urgency=Urgency.IMMEDIATE,
                    )
                )
            else:
                batched.append(record)

        if batched:
            events.append(
                NotificationEvent.for_records(
                    batched, app_base_url=self.settings.app_base_url, urgency=Urgency.BATCHED
                )
            )
        return events

    # ---------------------------------------------------------------- delivery
    def notify(
        self, records: Iterable[JobRecord], *, now: datetime | None = None
    ) -> NotificationOutcome:
        """Send alerts for ``records``, skipping any already notified."""
        outcome = NotificationOutcome()

        fresh: list[JobRecord] = []
        for record in records:
            if record.notification_sent:
                # The suppression that makes a second identical poll silent.
                outcome.skipped_job_ids.append(record.job_id)
                continue
            fresh.append(record)

        if not fresh:
            logger.info(
                "notify: nothing to send (%d already notified)", len(outcome.skipped_job_ids)
            )
            return outcome

        devices = self._devices()
        for event in self.plan(fresh):
            outcome.events.append(event)
            results = self.deliver(event, devices=devices)
            outcome.results.extend(results)

            if any(result.delivered for result in results):
                changed = self.repository.mark_notified(event.job_ids, now=now)
                outcome.notified_job_ids.extend(event.job_ids)
                logger.info(
                    "notified %s (marked %d): %s",
                    ", ".join(str(c.value) for c in sorted(self.settings_channels(results))),
                    changed,
                    summarize(event),
                )
            else:
                # Left pending on purpose: the next poll or a queue retry will
                # pick it up rather than the alert being lost.
                logger.warning("all channels failed for %s; left pending", summarize(event))
        return outcome

    def deliver(
        self, event: NotificationEvent, *, devices: Sequence[DeviceRegistration] | None = None
    ) -> list[DeliveryResult]:
        """Attempt every channel independently."""
        targets = devices if devices is not None else self._devices()
        return [self._send_email(event), self._send_push(event, targets)]

    def _send_email(self, event: NotificationEvent) -> DeliveryResult:
        message = format_email(event)
        try:
            receipt = self.email.send(
                message,
                to=self.settings.email.recipient,
                sender=self.settings.email.sender,
            )
        except (DeliveryError, TransportUnavailable) as exc:
            logger.warning("email delivery failed: %s", exc)
            return DeliveryResult.failed(Channel.EMAIL, event.job_ids, exc)
        except Exception as exc:
            logger.exception("unexpected email transport error")
            return DeliveryResult.failed(Channel.EMAIL, event.job_ids, exc)
        return DeliveryResult.ok(Channel.EMAIL, event.job_ids, detail=receipt)

    def _send_push(
        self, event: NotificationEvent, devices: Sequence[DeviceRegistration]
    ) -> DeliveryResult:
        message = format_push(event)
        try:
            receipts = self.push.send(message, devices=devices)
        except (DeliveryError, TransportUnavailable) as exc:
            logger.warning("push delivery failed: %s", exc)
            return DeliveryResult.failed(Channel.PUSH, event.job_ids, exc)
        except Exception as exc:
            logger.exception("unexpected push transport error")
            return DeliveryResult.failed(Channel.PUSH, event.job_ids, exc)
        return DeliveryResult.ok(Channel.PUSH, event.job_ids, detail=",".join(receipts))

    # ------------------------------------------------------------------ helpers
    def _devices(self) -> list[DeviceRegistration]:
        if self.devices is None:
            # No device registry configured: transports that fan out themselves
            # (SNS topics, the console sink) do not need one.
            return []
        return self.devices.enabled_devices()

    @staticmethod
    def settings_channels(results: Sequence[DeliveryResult]) -> set[Channel]:
        return {result.channel for result in results if result.delivered}

    def drain_pending(
        self, *, limit: int = 100, now: datetime | None = None
    ) -> NotificationOutcome:
        """Retry every record still awaiting an alert.

        The backstop for a crash between persisting a job and delivering its
        alert: the record is on the pending index, so the next poll finds it.
        """
        return self.notify(self.repository.pending_notifications(limit=limit), now=now)


__all__: Sequence[str] = ("NotificationOutcome", "Notifier")
