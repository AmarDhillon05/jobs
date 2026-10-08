"""Level 8 - applying from the phone: alert -> Apply kit -> draft -> save -> reuse.

The story the kit exists for, through the real pipeline, notifier, digest and kit
router, over both storage implementations:

1. a new internship is polled and the push that reaches the phone carries a
   signed kit link (and tapping it opens the kit, not a bare careers page);
2. that exact link opens the kit for that job, with the job and the profile;
3. a pasted question is drafted (fake drafter: no network, no cost), edited and
   saved for reuse, then edited and saved again;
4. the next job asks the same question: the saved answer comes back without a
   new draft;
5. the hourly digest links each internship to its own kit.

Nothing is ever submitted: the kit only prepares answers (PRD §33).
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from tests.e2e.conftest import T0, System, fixture_company, posting

from jobmonitor.api.routes import Request, Response
from jobmonitor.apply.drafter import SampleDrafter
from jobmonitor.apply.kit_api import KitApp
from jobmonitor.apply.links import KitLinker
from jobmonitor.apply.profile import Profile
from jobmonitor.apply.service import KitService
from jobmonitor.apply.store import InMemoryKitStore
from jobmonitor.config import PushSettings
from jobmonitor.notifications.transports import NtfyPushTransport

pytestmark = pytest.mark.e2e

SECRET = "e2e-kit-secret"
API = "https://api.test"
PROFILE = Profile.from_json(
    (Path(__file__).resolve().parents[2] / "apply" / "profile.example.json").read_text()
)
QUESTION = "Why do you want to intern at TestCo?"


class Phone:
    """What the phone does with a kit link: open it, and call the page's endpoints."""

    def __init__(self, system: System) -> None:
        self.drafter = SampleDrafter()
        self.app = KitApp(
            KitService(
                repository=system.repository,
                store=InMemoryKitStore(),
                profile=lambda: PROFILE,
                drafter=lambda: self.drafter,
                resume_available=lambda: True,
            ),
            secret=SECRET,
            resume_url=lambda: "https://s3.test/resume.pdf?sig=1",
        )

    def open(self, kit_url: str, action: str = "", body: Any = None) -> Response:
        parts = urlsplit(kit_url)
        assert f"{parts.scheme}://{parts.netloc}" == API
        path = parts.path + (f"/{action}" if action else "")
        return self.app.handle(
            Request(
                method="POST" if body is not None else "GET",
                path=path,
                query={k: v[0] for k, v in parse_qs(parts.query).items()},
                body=json.dumps(body) if body is not None else "",
            )
        )


@pytest.fixture
def kit_system(system: System) -> System:
    linker = KitLinker(API, SECRET)
    system.notifier.kit_link = linker
    system.digest.kit_link = linker
    return system


def test_alert_to_kit_to_saved_answer_reused_on_the_next_job(kit_system: System) -> None:
    system, phone = kit_system, Phone(kit_system)

    # 1. The alert: the push names the job and carries its signed kit link.
    first = system.poll([fixture_company(jobs=[posting("1")])]).new_records[0]
    payload = system.push_payloads[0]
    assert payload["job_id"] == first.job_id
    kit_url = payload["kit_url"]
    assert unquote(urlsplit(kit_url).path.rsplit("/kit/", 1)[1]) == first.job_id
    ntfy = NtfyPushTransport(PushSettings(transport="ntfy", ntfy_topic="t"))
    message = system.push.sent[0][0]
    assert ntfy.payload(message)["click"] == kit_url  # tapping opens the kit

    # 2. The kit opens for that job, with the profile ready to copy.
    page = phone.open(kit_url)
    assert page.status == 200 and page.content_type.startswith("text/html")
    html = page.payload()
    assert first.title in html and first.company in html
    assert PROFILE.value("email") in html
    assert first.url in html  # "Open application" goes to the real posting
    assert phone.open(kit_url, "resume").status == 302

    # A link for one job never opens another.
    forged = kit_url.replace("testco%3A1", "testco%3A2")
    assert forged != kit_url and phone.open(forged).status == 404

    # 3. Draft, edit, save for reuse; edit and save again.
    drafted = phone.open(kit_url, "draft", {"question": QUESTION}).payload()
    assert json.loads(drafted)["answer"]["source"] == "draft"
    assert len(phone.drafter.calls) == 1
    for text in ("I build tools people use every day.", "I build developer tools; v2."):
        saved = phone.open(
            kit_url, "answers", {"question": QUESTION, "answer": text, "save_to_library": True}
        )
        assert saved.status == 200
    reopened = json.loads(phone.open(kit_url, "draft", {"question": QUESTION}).payload())
    assert reopened["answer"]["text"] == "I build developer tools; v2."
    assert reopened["answer"]["source"] == "user"

    # 4. The next job asks the same question: the saved answer, no new draft.
    later = T0 + timedelta(minutes=10)
    second = system.poll([fixture_company(jobs=[posting("1"), posting("2")])], at=later)
    assert [r.job_id for r in second.new_records] == ["testco:2"]
    next_kit = system.push_payloads[-1]["kit_url"]
    assert next_kit != kit_url
    reused = json.loads(phone.open(next_kit, "draft", {"question": QUESTION}).payload())
    assert reused["answer"]["text"] == "I build developer tools; v2."
    assert reused["answer"]["source"] == "library"
    assert len(phone.drafter.calls) == 1

    # The repeated poll of job 1 sent no second alert.
    assert len(system.push_payloads) == 2

    # 5. The digest links each internship to its own kit.
    digest = system.send_digest()
    assert digest.sent
    body = system.email.sent[-1][0].text_body
    assert f"Apply kit: {kit_url}" in body and f"Apply kit: {next_kit}" in body
