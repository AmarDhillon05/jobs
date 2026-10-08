"""Apply-kit links in alerts: ntfy, email and the digest (Level 4).

With the kit configured, tapping an internship alert opens its kit; without it,
every alert is exactly as before. Events never get a kit (there is no application).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from tests.conftest import make_company

from jobmonitor.apply import tokens
from jobmonitor.apply.links import KitLinker
from jobmonitor.config import PushSettings, for_tests
from jobmonitor.models.job import EVENT_TYPE, Job
from jobmonitor.models.record import JobRecord
from jobmonitor.notifications import MemoryEmailTransport, Notifier
from jobmonitor.notifications.digest import format_digest
from jobmonitor.notifications.events import JobAlert, NotificationEvent, Urgency
from jobmonitor.notifications.formatters import format_email, format_push
from jobmonitor.notifications.transports import NtfyPushTransport
from jobmonitor.storage.memory import InMemoryJobRepository

pytestmark = pytest.mark.integration

T0 = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
APP = "https://app.test"
LINK = KitLinker("https://api.test/", "kit-secret")


def record(
    title: str = "Summer 2027 Intern - Software Engineer", employment_type: str | None = None
) -> JobRecord:
    job = Job(
        company="Salesforce",
        title=title,
        url="https://salesforce.wd12.myworkdayjobs.com/en-US/External/job/x_JR1",
        source="workday",
        external_id=title,
        location="San Francisco, CA",
        employment_type=employment_type,
    )
    return JobRecord.from_job(job, relevance_score=80, company=make_company("Salesforce"), now=T0)


def single(rec: JobRecord, kit_link: KitLinker | None = LINK) -> NotificationEvent:
    return NotificationEvent.for_records(
        [rec], app_base_url=APP, urgency=Urgency.IMMEDIATE, kit_link=kit_link
    )


class TestLinks:
    def test_a_signed_link_per_job(self) -> None:
        url = LINK("salesforce:JR1")
        parts = urlsplit(url)
        assert url.startswith("https://api.test/kit/salesforce%3AJR1?t=")
        job_id = unquote(parts.path.split("/kit/")[1])
        assert tokens.verify(job_id, parse_qs(parts.query)["t"][0], "kit-secret")

    def test_from_env(self) -> None:
        assert KitLinker.from_env({}) is None
        assert KitLinker.from_env({"KIT_BASE_URL": "https://api.test"}) is None  # no secret
        linker = KitLinker.from_env({"KIT_BASE_URL": "https://api.test", "KIT_SECRET": "s"})
        assert linker == KitLinker("https://api.test", "s")


class TestAlert:
    def test_internships_get_a_kit_and_events_do_not(self) -> None:
        job = JobAlert.from_record(record(), app_base_url=APP, kit_link=LINK)
        assert job.kit_url == LINK(job.job_id)
        assert JobAlert.from_dict(job.to_dict()) == job
        event = JobAlert.from_record(
            record("Info Session", EVENT_TYPE), app_base_url=APP, kit_link=LINK
        )
        assert event.kit_url is None
        assert JobAlert.from_record(record(), app_base_url=APP).kit_url is None


class TestNtfy:
    def transport(self) -> NtfyPushTransport:
        return NtfyPushTransport(PushSettings(transport="ntfy", ntfy_topic="t"))

    def test_tap_opens_the_kit(self) -> None:
        rec = record()
        payload = self.transport().payload(format_push(single(rec)))
        kit = LINK(rec.job_id)
        assert payload["click"] == kit
        assert [a["label"] for a in payload["actions"]] == [
            "Apply kit",
            "Open application",
            "Copy link",
        ]
        assert payload["actions"][0]["url"] == kit
        assert payload["actions"][1]["url"] == rec.url

    def test_without_the_kit_nothing_changes(self) -> None:
        rec = record()
        payload = self.transport().payload(format_push(single(rec, kit_link=None)))
        assert payload["click"] == rec.url
        assert [a["label"] for a in payload["actions"]] == ["Open application", "Copy link"]


class TestEmailAndDigest:
    def test_email(self) -> None:
        rec = record()
        message = format_email(single(rec))
        assert "Apply kit (your answers, ready to paste):" in message.text_body
        assert LINK(rec.job_id) in message.text_body
        assert ">Apply kit</a>" in message.html_body

    def test_digest_links_internships_only(self) -> None:
        job, event = record(), record("Info Session · Oct 20, 2026", EVENT_TYPE)
        message = format_digest(
            [job, event],
            window_start=T0,
            window_end=T0 + timedelta(hours=1),
            strong=55,
            kit_link=LINK,
        )
        assert f"Apply kit: {LINK(job.job_id)}" in message.text_body
        assert LINK(event.job_id) not in message.text_body
        assert message.html_body.count(">Apply kit</a>") == 1

    def test_the_notifier_signs_links_when_configured(self) -> None:
        repository = InMemoryJobRepository()
        notifier = Notifier(
            for_tests(app_base_url=APP),
            repository=repository,
            email_transport=MemoryEmailTransport(),
            kit_link=LINK,
        )
        rec = record()
        (event,) = notifier.plan([rec])
        assert event.jobs[0].kit_url == LINK(rec.job_id)
