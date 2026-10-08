"""The hourly email digest.

Instant alerts go to the phone (push). Email is one summary per window - an hour
by default - listing every job first seen in that window, strong matches first.
An hour in which nothing was found sends **nothing**: an empty email every hour
would train the reader to ignore the ones that matter.

The window is taken from the schedule's own timestamp, floored to a multiple of
the window length, so consecutive runs cover consecutive, non-overlapping
windows even when an invocation starts a little late: a run at 14:02 covers
13:00-14:00, the next at 15:01 covers 14:00-15:00.

The digest reads ``first_seen`` from storage and nothing else, so it is
independent of whether push delivered: a job alerted instantly still appears in
the hour's summary, and a job whose push failed is not lost from email.
"""

from __future__ import annotations

import html
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from jobmonitor.config import Settings
from jobmonitor.models.job import is_event_kind, posting_kind
from jobmonitor.models.record import JobRecord
from jobmonitor.notifications.formatters import KIND_LABELS, EmailMessage
from jobmonitor.notifications.transports import EmailTransport
from jobmonitor.storage.base import JobRepository

logger = logging.getLogger(__name__)

#: Upper bound on jobs read for one window. Far above a real hour's findings.
MAX_DIGEST_JOBS = 1000


def digest_window(now: datetime, minutes: int) -> tuple[datetime, datetime]:
    """The most recent *complete* window ending at or before ``now``."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    length = timedelta(minutes=minutes)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    end = epoch + ((now - epoch) // length) * length
    return end - length, end


@dataclass(slots=True)
class DigestOutcome:
    window_start: datetime
    window_end: datetime
    jobs: list[JobRecord] = field(default_factory=list)
    sent: bool = False
    receipt: str | None = None

    @property
    def skipped(self) -> bool:
        return not self.jobs

    def to_dict(self) -> dict[str, object]:
        return {
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
            "jobs": len(self.jobs),
            "sent": self.sent,
            "skipped_empty": self.skipped,
        }


def _sorted(records: Sequence[JobRecord]) -> list[JobRecord]:
    return sorted(records, key=lambda r: (-r.relevance_score, r.company.lower(), r.title.lower()))


def _fmt_time(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def format_digest(
    records: Sequence[JobRecord],
    *,
    window_start: datetime,
    window_end: datetime,
    strong: int,
    kit_link: Callable[[str], str] | None = None,
) -> EmailMessage:
    """Render the window's jobs, then its events and programs.

    ``strong`` is the push threshold: internships at or above it were also sent
    to the phone; the rest were kept because the filter is deliberately loose,
    and are listed separately. Events and programs get their own section, after
    the jobs, recruiting ones (scored higher) first.
    """
    if not records:
        raise ValueError("a digest needs at least one job; empty windows are skipped")
    ordered = _sorted(records)
    jobs = [r for r in ordered if not is_event_kind(r.employment_type)]
    events = [r for r in ordered if is_event_kind(r.employment_type)]
    strong_jobs = [r for r in jobs if r.relevance_score >= strong]
    other_jobs = [r for r in jobs if r.relevance_score < strong]

    companies = sorted({r.company for r in ordered}, key=str.lower)
    shown = ", ".join(companies[:3]) + (
        f" +{len(companies) - 3} more" if len(companies) > 3 else ""
    )
    counts = [
        part
        for part in (
            _plural(len(jobs), "new internship") if jobs else "",
            _plural(len(events), "event") if events else "",
        )
        if part
    ]
    summary = ", ".join(counts)
    tag = "Internships" if jobs else "Events"
    subject = f"[{tag}] {summary} - {shown}"
    span = f"{_fmt_time(window_start)} - {_fmt_time(window_end)}"

    def label(record: JobRecord) -> str:
        return KIND_LABELS[posting_kind(record.employment_type)]

    def text_line(record: JobRecord) -> str:
        where = f" ({record.location})" if record.location else ""
        if is_event_kind(record.employment_type):
            return f"- {record.company}: {record.title}{where}\n  {label(record)}\n  {record.url}"
        posted = f", posted {record.date_posted.date().isoformat()}" if record.date_posted else ""
        kit = f"\n  Apply kit: {kit_link(record.job_id)}" if kit_link else ""
        return (
            f"- {record.company}: {record.title}{where}\n"
            f"  relevance {record.relevance_score}/100{posted}\n"
            f"  {record.url}{kit}"
        )

    text_parts = [f"{summary} first seen {span}.", ""]
    if strong_jobs:
        text_parts += ["STRONG MATCHES", *(text_line(r) for r in strong_jobs), ""]
    if other_jobs:
        text_parts += ["ALSO FOUND (lower relevance)", *(text_line(r) for r in other_jobs), ""]
    if events:
        text_parts += ["EVENTS & PROGRAMS", *(text_line(r) for r in events), ""]
    text_body = "\n".join(text_parts).rstrip() + "\n"

    def html_row(record: JobRecord) -> str:
        e = html.escape
        where = f" &middot; {e(record.location)}" if record.location else ""
        is_event = is_event_kind(record.employment_type)
        detail = e(label(record)) if is_event else f"{record.relevance_score}/100"
        kit = (
            f' &middot; <a href="{e(kit_link(record.job_id))}" style="color:#1a56db">Apply kit</a>'
            if kit_link and not is_event
            else ""
        )
        return (
            '<li style="margin:0 0 12px">'
            f"<strong>{e(record.company)}</strong>: {e(record.title)}"
            f'<span style="color:#666">{where} &middot; {detail}</span><br>'
            f'<a href="{e(record.url)}" style="color:#1a56db">{e(record.url)}</a>{kit}</li>'
        )

    def html_section(title: str, rows: Sequence[JobRecord]) -> str:
        if not rows:
            return ""
        items = "".join(html_row(r) for r in rows)
        return (
            f'<h2 style="font-size:16px;margin:20px 0 8px">{html.escape(title)}</h2>'
            f'<ul style="padding-left:18px;margin:0">{items}</ul>'
        )

    html_body = (
        '<div style="max-width:640px;margin:0 auto;padding:20px;font-family:system-ui,sans-serif">'
        f'<h1 style="font-size:20px;margin:0 0 4px">{html.escape(summary)}</h1>'
        f'<p style="color:#666;margin:0">First seen {html.escape(span)}</p>'
        + html_section("Strong matches", strong_jobs)
        + html_section("Also found (lower relevance)", other_jobs)
        + html_section("Events & programs", events)
        + '<p style="color:#888;font-size:12px;margin-top:24px">Sent by your internship '
        "monitor. Hours with nothing new send no email.</p></div>"
    )
    return EmailMessage(subject=subject, text_body=text_body, html_body=html_body)


class DigestSender:
    def __init__(
        self,
        settings: Settings,
        *,
        repository: JobRepository,
        email_transport: EmailTransport,
        kit_link: Callable[[str], str] | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.email = email_transport
        self.kit_link = kit_link

    def jobs_in(self, start: datetime, end: datetime) -> list[JobRecord]:
        records = self.repository.recent(limit=MAX_DIGEST_JOBS, since=start)
        # Baseline records were already listed before their source was monitored
        # (the quiet first poll of an event source): not news, so not in the email.
        return [r for r in records if start <= r.first_seen < end and not r.baseline]

    def send(
        self,
        *,
        now: datetime | None = None,
        window: tuple[datetime, datetime] | None = None,
    ) -> DigestOutcome:
        """Email the last complete window's jobs, or nothing if there were none.

        ``window`` overrides the scheduled window (the CLI uses it to summarise
        one in-process poll). A delivery failure propagates, so the scheduler's
        retry runs it again.
        """
        start, end = window or digest_window(
            now or datetime.now(UTC), self.settings.email.digest_window_minutes
        )
        outcome = DigestOutcome(window_start=start, window_end=end, jobs=self.jobs_in(start, end))
        if outcome.skipped:
            logger.info("digest: nothing first seen %s - %s; no email", start, end)
            return outcome
        message = format_digest(
            outcome.jobs,
            window_start=start,
            window_end=end,
            strong=self.settings.filters.notify_threshold,
            kit_link=self.kit_link,
        )
        outcome.receipt = self.email.send(
            message, to=self.settings.email.recipient, sender=self.settings.email.sender
        )
        outcome.sent = True
        logger.info("digest: emailed %d job(s) for %s - %s", len(outcome.jobs), start, end)
        return outcome


__all__: Sequence[str] = (
    "MAX_DIGEST_JOBS",
    "DigestOutcome",
    "DigestSender",
    "digest_window",
    "format_digest",
)
