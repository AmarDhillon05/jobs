"""Rendering a notification for each channel (PRD §22).

The email body follows the PRD's example layout verbatim for a single job, and
groups cleanly when a poll finds several. The push payload is deliberately small -
push services cap payload size, and the client re-fetches detail from the API
anyway - but it always carries ``job_id`` and ``deep_link``, because that is what
makes tapping a notification land on the right job.
"""

from __future__ import annotations

import html
from collections.abc import Sequence
from dataclasses import dataclass

from jobmonitor.notifications.events import JobAlert, NotificationEvent, Urgency

MAX_PUSH_BODY_CHARS = 180
"""Push services reject oversized payloads; truncate rather than fail to deliver."""

MAX_PUSH_JOBS = 5
"""Ids carried in a push payload. More than this and the client should just open
the feed."""

#: How each kind of posting is named in alerts.
KIND_LABELS: dict[str, str] = {
    "internship": "new internship",
    "event": "new event",
    "program": "new program",
    "industry_event": "industry event",
}
#: The heading of one item in an email, by kind.
KIND_HEADERS: dict[str, str] = {
    "internship": "NEW INTERNSHIP",
    "event": "NEW EVENT",
    "program": "NEW PROGRAM",
    "industry_event": "INDUSTRY EVENT",
}


@dataclass(frozen=True, slots=True)
class EmailMessage:
    subject: str
    text_body: str
    html_body: str

    @property
    def job_count(self) -> int:
        return sum(self.text_body.count(header) for header in KIND_HEADERS.values())


@dataclass(frozen=True, slots=True)
class PushMessage:
    title: str
    body: str
    #: Structured data the client reads on tap. Values are strings so the payload
    #: survives transports that only carry string maps (SNS message attributes).
    data: dict[str, str]

    @property
    def deep_link(self) -> str:
        return self.data.get("deep_link", "")


def _location(alert: JobAlert) -> str:
    return alert.location or "Location not specified"


def _role_label(alert: JobAlert) -> str:
    return "Program" if alert.kind == "program" else "Event" if alert.is_event else "Role"


def _format_one_text(alert: JobAlert) -> str:
    lines = [
        KIND_HEADERS.get(alert.kind, "NEW INTERNSHIP"),
        "",
        f"Company: {alert.company}",
        f"{_role_label(alert)}: {alert.title}",
        f"Location: {_location(alert)}",
        f"First Seen: {alert.first_seen or 'unknown'}",
    ]
    # "Posted: if available" - omitted entirely when the source gave us nothing,
    # rather than printing an empty field.
    if alert.date_posted:
        lines.append(f"Posted: {alert.date_posted}")
    if not alert.is_event:
        lines.append(f"Relevance: {alert.relevance_score}/100")
    if alert.description_preview:
        lines += ["", alert.description_preview]
    action = "Details:" if alert.is_event else "Apply:"
    lines += ["", action, alert.url, "", "Open in app:", alert.deep_link]
    return "\n".join(lines)


def _format_one_html(alert: JobAlert) -> str:
    escape = html.escape
    rows = [
        ("Company", escape(alert.company)),
        (_role_label(alert), escape(alert.title)),
        ("Location", escape(_location(alert))),
        ("First seen", escape(alert.first_seen or "unknown")),
    ]
    if alert.date_posted:
        rows.append(("Posted", escape(alert.date_posted)))
    if not alert.is_event:
        rows.append(("Relevance", f"{alert.relevance_score}/100"))
    cells = "".join(
        f'<tr><td style="padding:2px 12px 2px 0;color:#666">{label}</td>'
        f'<td style="padding:2px 0"><strong>{value}</strong></td></tr>'
        for label, value in rows
    )
    preview = (
        f'<p style="color:#444">{escape(alert.description_preview)}</p>'
        if alert.description_preview
        else ""
    )
    return (
        '<div style="margin:0 0 28px;font-family:system-ui,sans-serif">'
        f'<h2 style="margin:0 0 8px;font-size:17px">{escape(alert.title)}</h2>'
        f'<table style="border-collapse:collapse;font-size:14px">{cells}</table>'
        f"{preview}"
        f'<p><a href="{escape(alert.url)}" '
        'style="display:inline-block;padding:9px 16px;background:#1a56db;color:#fff;'
        f'border-radius:6px;text-decoration:none">{_button(alert)}</a>'
        f'&nbsp;&nbsp;<a href="{escape(alert.deep_link)}" style="color:#1a56db">View in app</a></p>'
        "</div>"
    )


def _button(alert: JobAlert) -> str:
    if alert.kind == "program":
        return "Open Program"
    return "Open Event" if alert.is_event else "Open Application"


def _found(alerts: Sequence[JobAlert]) -> str:
    """ "3 new internships", "2 events", "1 new internship and 2 events"."""
    jobs = sum(1 for alert in alerts if not alert.is_event)
    events = len(alerts) - jobs
    parts = []
    if jobs:
        parts.append(f"{jobs} new internship" + ("" if jobs == 1 else "s"))
    if events:
        parts.append(f"{events} event" + ("" if events == 1 else "s"))
    return " and ".join(parts)


def format_email(event: NotificationEvent) -> EmailMessage:
    """Render an event as an email, single or grouped."""
    alerts = event.jobs
    if event.is_single:
        alert = alerts[0]
        tag = {"program": "Program", "event": "Event", "industry_event": "Event"}.get(
            alert.kind, "Internship"
        )
        subject = f"[{tag}] {alert.company} - {alert.title}"
    else:
        companies = sorted({alert.company for alert in alerts})
        shown = ", ".join(companies[:3])
        if len(companies) > 3:
            shown += f" +{len(companies) - 3} more"
        if all(alert.is_event for alert in alerts):
            subject = f"[Events] {len(alerts)} new events - {shown}"
        else:
            subject = f"[Internships] {len(alerts)} new roles - {shown}"

    text_sections = [_format_one_text(alert) for alert in alerts]
    text_body = ("\n\n" + "-" * 56 + "\n\n").join(text_sections)
    if not event.is_single:
        text_body = f"{_found(alerts)} found in this poll.\n\n" + "=" * 56 + "\n\n" + text_body

    heading = (
        KIND_LABELS.get(alerts[0].kind, "new internship").capitalize()
        if event.is_single
        else f"{_found(alerts)} found in this poll"
    )
    html_body = (
        '<div style="max-width:640px;margin:0 auto;padding:20px">'
        f'<h1 style="font-size:20px;margin:0 0 20px">{html.escape(heading)}</h1>'
        + "".join(_format_one_html(alert) for alert in alerts)
        + '<p style="color:#888;font-size:12px">Sent by your internship monitor. '
        "Relevance scores are advisory.</p></div>"
    )
    return EmailMessage(subject=subject, text_body=text_body, html_body=html_body)


def format_push(event: NotificationEvent) -> PushMessage:
    """Render an event as a push notification."""
    alerts = event.jobs
    if event.is_single:
        alert = alerts[0]
        title = f"{alert.company} - {KIND_LABELS.get(alert.kind, 'new internship')}"
        body = alert.title
        if alert.location:
            body = f"{body} ({alert.location})"
        deep_link = alert.deep_link
    else:
        companies = sorted({alert.company for alert in alerts})
        title = _found(alerts)
        body = ", ".join(f"{alert.company}: {alert.title}" for alert in alerts[:3])
        if len(alerts) > 3:
            body += f" +{len(alerts) - 3} more"
        # A grouped alert opens the feed; individual jobs are one tap further.
        deep_link = _feed_link(alerts[0].deep_link)
        _ = companies

    if len(body) > MAX_PUSH_BODY_CHARS:
        body = body[: MAX_PUSH_BODY_CHARS - 3].rstrip() + "..."

    data = {
        "schema_version": str(event.schema_version),
        "urgency": event.urgency.value,
        "deep_link": deep_link,
        "job_count": str(len(alerts)),
        "job_ids": ",".join(event.job_ids[:MAX_PUSH_JOBS]),
    }
    if event.is_single:
        data["job_id"] = alerts[0].job_id
        data["apply_url"] = alerts[0].url
        data["kind"] = alerts[0].kind
    return PushMessage(title=title, body=body, data=data)


def _feed_link(job_deep_link: str) -> str:
    """Turn ``.../jobs/<id>`` into the feed root, for grouped alerts."""
    marker = "/jobs/"
    if marker in job_deep_link:
        return job_deep_link.split(marker)[0] + "/"
    return job_deep_link


def summarize(event: NotificationEvent) -> str:
    """One-line log summary."""
    urgency = "immediate" if event.urgency is Urgency.IMMEDIATE else "batched"
    return f"{len(event.jobs)} job(s), {urgency}: {', '.join(event.job_ids[:5])}"


__all__: Sequence[str] = (
    "KIND_HEADERS",
    "KIND_LABELS",
    "MAX_PUSH_BODY_CHARS",
    "MAX_PUSH_JOBS",
    "EmailMessage",
    "PushMessage",
    "format_email",
    "format_push",
    "summarize",
)
