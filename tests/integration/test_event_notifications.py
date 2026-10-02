"""How events and programs are labelled in alerts (Level 4).

Events ride the same ntfy push and hourly digest as internships, and must be
easy to tell apart: "new event" / "new program" / "industry event" instead of
"new internship", their own email headings, and their own digest section.
Recruiting events and programs ring at ntfy priority 4 whatever the company's
tier; industry events at 3.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from tests.conftest import make_company

from jobmonitor.config import PushSettings, for_tests
from jobmonitor.models.company import Priority
from jobmonitor.models.job import EVENT_TYPE, INDUSTRY_EVENT_TYPE, PROGRAM_TYPE, Job
from jobmonitor.models.record import JobRecord
from jobmonitor.notifications import MemoryEmailTransport, MemoryPushTransport, Notifier
from jobmonitor.notifications.digest import format_digest
from jobmonitor.notifications.events import JobAlert, NotificationEvent, Urgency
from jobmonitor.notifications.formatters import format_email, format_push
from jobmonitor.notifications.transports import NtfyPushTransport
from jobmonitor.storage.memory import InMemoryJobRepository

pytestmark = pytest.mark.integration

T0 = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
APP = "https://app.test"


def record(
    title: str,
    employment_type: str | None = None,
    *,
    company: str = "Jane Street",
    priority: Priority = Priority.HIGH,
    score: int = 80,
    identifier: str | None = None,
) -> JobRecord:
    job = Job(
        company=company,
        title=title,
        url=f"https://example.com/{identifier or title.replace(' ', '-').lower()}",
        source="event_page",
        external_id=identifier or title,
        location="New York, NY",
        employment_type=employment_type,
    )
    return JobRecord.from_job(
        job, relevance_score=score, company=make_company(company, priority=priority), now=T0
    )


def single(rec: JobRecord, urgency: Urgency = Urgency.IMMEDIATE) -> NotificationEvent:
    return NotificationEvent.for_records([rec], app_base_url=APP, urgency=urgency)


class TestAlertPayload:
    def test_kind_round_trips_and_defaults_to_internship(self) -> None:
        alert = JobAlert.from_record(record("Bridge · Nov 4, 2026", PROGRAM_TYPE), app_base_url=APP)
        assert alert.kind == "program" and alert.is_event
        assert JobAlert.from_dict(alert.to_dict()) == alert
        legacy = alert.to_dict()
        del legacy["kind"]  # a payload from before events existed
        assert JobAlert.from_dict(legacy).kind == "internship"

    @pytest.mark.parametrize(
        ("employment_type", "label"),
        [
            (None, "Jane Street - new internship"),
            (EVENT_TYPE, "Jane Street - new event"),
            (PROGRAM_TYPE, "Jane Street - new program"),
            (INDUSTRY_EVENT_TYPE, "Jane Street - industry event"),
        ],
    )
    def test_push_titles(self, employment_type: str | None, label: str) -> None:
        message = format_push(single(record("Bridge", employment_type)))
        assert message.title == label
        assert message.data["kind"] == (
            {None: "internship", EVENT_TYPE: "event", PROGRAM_TYPE: "program"}.get(
                employment_type, "industry_event"
            )
        )

    def test_grouped_push_counts_jobs_and_events(self) -> None:
        event = NotificationEvent.for_records(
            [
                record("Software Engineer Intern", identifier="1"),
                record("Info Session", EVENT_TYPE, identifier="2"),
                record("Summit", INDUSTRY_EVENT_TYPE, identifier="3"),
            ],
            app_base_url=APP,
        )
        assert format_push(event).title == "1 new internship and 2 events"


class TestNtfy:
    def transport(self) -> NtfyPushTransport:
        return NtfyPushTransport(PushSettings(transport="ntfy", ntfy_topic="test-topic"))

    def test_event_payload(self) -> None:
        payload = self.transport().payload(format_push(single(record("Info Session", EVENT_TYPE))))
        assert payload["priority"] == 4
        assert payload["tags"] == ["date"]
        assert payload["actions"][0]["label"] == "Open event"
        assert payload["click"] == "https://example.com/info-session"

    def test_program_payload(self) -> None:
        payload = self.transport().payload(format_push(single(record("Bridge", PROGRAM_TYPE))))
        assert payload["tags"] == ["mortar_board"]
        assert payload["actions"][0]["label"] == "Open program"

    def test_internship_payload_is_unchanged(self) -> None:
        payload = self.transport().payload(format_push(single(record("SWE Intern"))))
        assert payload["tags"] == ["briefcase"]
        assert payload["actions"][0]["label"] == "Open application"


class TestUrgency:
    def plan(self, *records: JobRecord) -> list[NotificationEvent]:
        settings = for_tests(app_base_url=APP)
        notifier = Notifier(
            settings,
            repository=InMemoryJobRepository(),
            email_transport=MemoryEmailTransport(),
            push_transport=MemoryPushTransport(),
        )
        return notifier.plan(list(records))

    def test_recruiting_events_and_programs_are_immediate_at_any_tier(self) -> None:
        for employment_type in (EVENT_TYPE, PROGRAM_TYPE):
            events = self.plan(
                record("Coffee Chat", employment_type, priority=Priority.EXPERIMENTAL)
            )
            assert [e.urgency for e in events] == [Urgency.IMMEDIATE]

    def test_industry_events_are_never_immediate(self) -> None:
        events = self.plan(record("DevConnect", INDUSTRY_EVENT_TYPE, priority=Priority.HIGH))
        assert [e.urgency for e in events] == [Urgency.BATCHED]

    def test_industry_events_ring_at_default_priority_on_ntfy(self) -> None:
        settings = for_tests(app_base_url=APP)
        ntfy = NtfyPushTransport(PushSettings(transport="ntfy", ntfy_topic="t"))
        notifier = Notifier(
            settings,
            repository=InMemoryJobRepository(),
            email_transport=MemoryEmailTransport(),
            push_transport=ntfy,
        )
        (event,) = notifier.plan([record("DevConnect", INDUSTRY_EVENT_TYPE)])
        assert ntfy.payload(format_push(event))["priority"] == 3


class TestEmail:
    def test_single_event_email(self) -> None:
        message = format_email(single(record("Info Session · Oct 20, 2026", EVENT_TYPE)))
        assert message.subject == "[Event] Jane Street - Info Session · Oct 20, 2026"
        assert message.text_body.startswith("NEW EVENT")
        assert "Event: Info Session" in message.text_body
        assert "Relevance:" not in message.text_body
        assert "Details:" in message.text_body and "Apply:" not in message.text_body
        assert "Open Event" in message.html_body
        assert message.job_count == 1

    def test_program_and_industry_headings(self) -> None:
        assert format_email(single(record("Bridge", PROGRAM_TYPE))).text_body.startswith(
            "NEW PROGRAM"
        )
        assert format_email(single(record("Summit", INDUSTRY_EVENT_TYPE))).text_body.startswith(
            "INDUSTRY EVENT"
        )

    def test_mixed_email_counts_every_item(self) -> None:
        event = NotificationEvent.for_records(
            [
                record("Software Engineer Intern", identifier="1"),
                record("Info Session", EVENT_TYPE, identifier="2"),
            ],
            app_base_url=APP,
        )
        message = format_email(event)
        assert message.job_count == 2
        assert "1 new internship and 1 event found in this poll" in message.text_body


class TestDigest:
    def test_events_get_their_own_section_after_the_jobs(self) -> None:
        records = [
            record("Software Engineer Intern", identifier="1", score=90),
            record("Info Session · Oct 20, 2026", EVENT_TYPE, identifier="2"),
            record("DevConnect · Oct 14, 2026", INDUSTRY_EVENT_TYPE, identifier="3", score=60),
        ]
        message = format_digest(
            records, window_start=T0, window_end=T0 + timedelta(hours=1), strong=55
        )
        assert message.subject == "[Internships] 1 new internship, 2 events - Jane Street"
        text = message.text_body
        assert text.index("STRONG MATCHES") < text.index("EVENTS & PROGRAMS")
        events_section = text[text.index("EVENTS & PROGRAMS") :]
        assert "Info Session" in events_section and "DevConnect" in events_section
        assert "Software Engineer Intern" not in events_section
        assert events_section.index("Info Session") < events_section.index("DevConnect")
        assert "industry event" in events_section
        assert "Events &amp; programs" in message.html_body

    def test_events_only_digest(self) -> None:
        message = format_digest(
            [record("Info Session", EVENT_TYPE)],
            window_start=T0,
            window_end=T0 + timedelta(hours=1),
            strong=55,
        )
        assert message.subject == "[Events] 1 event - Jane Street"
        assert "STRONG MATCHES" not in message.text_body
