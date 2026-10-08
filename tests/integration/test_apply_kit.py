"""The Apply kit end to end, in process: routes, page, drafts, saved answers.

The kit store runs twice, in memory and as DynamoDB under Moto; the profile and
resume are read from S3 under Moto as the Kit Lambda reads them.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from tests.integration.conftest import AWS_TEST_SETTINGS
from tests.scrapers.conftest import build_client
from tests.support.http import FakeTransport, ScriptedResponse, load_fixture

from jobmonitor.api.routes import Request
from jobmonitor.apply import tokens
from jobmonitor.apply.drafter import DraftError, JobContext, SampleDrafter
from jobmonitor.apply.kit_api import KitApp, S3Files, handler
from jobmonitor.apply.profile import Profile
from jobmonitor.apply.service import KitService
from jobmonitor.apply.store import DynamoKitStore, InMemoryKitStore, KitStore
from jobmonitor.models.company import load_default_registry
from jobmonitor.models.job import EVENT_TYPE, Job
from jobmonitor.storage.memory import InMemoryJobRepository

pytestmark = pytest.mark.integration

SECRET = "test-kit-secret"
PROFILE = Profile.from_json(
    (Path(__file__).resolve().parents[2] / "apply" / "profile.example.json").read_text()
)
GREENHOUSE = load_fixture("apply", "greenhouse_questions_anthropic_fellows.json")


@pytest.fixture(params=["memory", "dynamodb-moto"])
def store(request: pytest.FixtureRequest) -> Iterator[KitStore]:
    if request.param == "memory":
        yield InMemoryKitStore()
        return
    resource = request.getfixturevalue("moto_dynamodb")
    yield DynamoKitStore(resource.Table(AWS_TEST_SETTINGS.apply_table))


class Kit:
    """A kit app over an in-memory job repository, with a counting drafter."""

    def __init__(self, store: KitStore, *, http: Any = None, limit: int = 60) -> None:
        self.repository = InMemoryJobRepository()
        self.store = store
        self.drafter = SampleDrafter()
        self.service = KitService(
            repository=self.repository,
            store=store,
            profile=lambda: PROFILE,
            drafter=lambda: self.drafter,
            registry=load_default_registry(),
            http=http,
            resume_available=lambda: True,
            draft_daily_limit=limit,
        )
        self.app = KitApp(
            self.service, secret=SECRET, resume_url=lambda: "https://s3.test/resume?sig=1"
        )

    def add(self, **overrides: Any) -> str:
        fields: dict[str, Any] = {
            "company": "Salesforce",
            "title": "Summer 2027 Intern - Software Engineer",
            "url": "https://salesforce.wd12.myworkdayjobs.com/en-US/External/job/x_JR1",
            "source": "workday",
            "external_id": "JR1",
            "location": "San Francisco, CA",
        }
        fields.update(overrides)
        return self.repository.upsert(Job(**fields)).record.job_id

    def call(
        self, method: str, job_id: str, action: str = "", body: Any = None, token: str | None = None
    ) -> Any:
        from urllib.parse import quote

        path = f"/kit/{quote(job_id, safe='')}" + (f"/{action}" if action else "")
        return self.app.handle(
            Request(
                method=method,
                path=path,
                query={"t": tokens.sign(job_id, SECRET) if token is None else token},
                body=json.dumps(body) if body is not None else "",
            )
        )


class TestAccess:
    def test_a_wrong_or_missing_token_is_a_plain_404(self) -> None:
        kit = Kit(InMemoryKitStore())
        job = kit.add()
        assert kit.call("GET", job).status == 200
        assert kit.call("GET", job, token="nope").status == 404
        assert kit.call("GET", job, token="").status == 404
        other = kit.add(external_id="JR2", url="https://salesforce.wd12.myworkdayjobs.com/x_JR2")
        assert kit.call("GET", other, token=tokens.sign(job, SECRET)).status == 404

    def test_unknown_jobs_and_events_have_no_kit(self) -> None:
        kit = Kit(InMemoryKitStore())
        assert kit.call("GET", "nobody:1").status == 404
        event = kit.add(external_id="event:1", title="Info Session", employment_type=EVENT_TYPE)
        assert kit.call("GET", event).status == 404

    def test_other_paths(self) -> None:
        kit = Kit(InMemoryKitStore())
        job = kit.add()
        assert kit.call("DELETE", job).status == 405
        assert kit.call("GET", job, "draft").status == 405
        assert kit.app.handle(Request(method="GET", path="/jobs")).status == 404


class TestPage:
    def test_workday_kit(self) -> None:
        kit = Kit(InMemoryKitStore())
        response = kit.call("GET", kit.add())
        assert response.status == 200
        assert response.content_type.startswith("text/html")
        headers = response.all_headers
        assert headers["Referrer-Policy"] == "no-referrer"
        assert headers["Cache-Control"] == "no-store"
        assert "noindex" in headers["X-Robots-Tag"]
        page = response.payload()
        for step in ("My Information", "My Experience", "Application Questions", "Self Identify"):
            assert step in page
        assert "alex.rivera@example.edu" in page
        assert "Select: No" in page  # sponsorship, as the option to pick
        assert "Use My Last Application" in page
        assert "Download resume" in page

    def test_everything_is_escaped(self) -> None:
        kit = Kit(InMemoryKitStore())
        job = kit.add(title='Intern <script>alert("x")</script>', external_id="JR9")
        page = kit.call("GET", job).payload()
        assert "<script>alert" not in page
        assert "&lt;script&gt;alert" in page

    def test_greenhouse_kit_uses_the_real_form_and_caches_it(self) -> None:
        transport = FakeTransport([ScriptedResponse.json(GREENHOUSE)])
        kit = Kit(InMemoryKitStore(), http=build_client(transport))
        job = kit.add(
            company="Anthropic",
            title="Anthropic Fellows Program, ML Systems & Reinforcement Learning",
            url="https://job-boards.greenhouse.io/anthropic/jobs/5183051008",
            source="greenhouse",
            external_id="5183051008",
        )
        page = kit.call("GET", job).payload()
        assert transport.urls == [
            "https://boards-api.greenhouse.io/v1/boards/anthropic/jobs/5183051008?questions=true"
        ]
        assert "Why Anthropic?" in page and "AI Policy for Application" in page
        assert 'data-autodraft="1"' in page  # the required free-text question drafts itself
        assert 'data-choice="Yes"' in page  # yes/no questions are one tap
        kit.call("GET", job)
        assert transport.call_count == 1  # cached

    def test_a_failed_question_fetch_falls_back_to_common_questions(self) -> None:
        kit = Kit(
            InMemoryKitStore(), http=build_client(FakeTransport(always=ScriptedResponse.error(404)))
        )
        job = kit.add(
            company="Anthropic",
            url="https://job-boards.greenhouse.io/anthropic/jobs/1",
            source="greenhouse",
            external_id="1",
        )
        page = kit.call("GET", job).payload()
        assert "Common questions" in page


class TestDrafts:
    def test_draft_is_stored_and_not_paid_for_twice(self, store: KitStore) -> None:
        kit = Kit(store)
        job = kit.add()
        first = kit.call("POST", job, "draft", {"question": "Why Salesforce?"})
        assert first.status == 200
        assert first.body["answer"]["source"] == "draft"
        assert "edit before using" in first.body["label"]
        again = kit.call("POST", job, "draft", {"question": "Why Salesforce?"})
        assert again.body["answer"]["text"] == first.body["answer"]["text"]
        assert len(kit.drafter.calls) == 1

    def test_redraft_improves_the_current_text(self, store: KitStore) -> None:
        kit = Kit(store)
        job = kit.add()
        kit.call("POST", job, "draft", {"question": "Why Salesforce?"})
        kit.call(
            "POST",
            job,
            "draft",
            {"question": "Why Salesforce?", "redraft": True, "current": "Slack!"},
        )
        assert kit.drafter.calls[-1]["current"] == "Slack!"
        assert len(kit.drafter.calls) == 2

    def test_daily_limit(self, store: KitStore) -> None:
        kit = Kit(store, limit=2)
        job = kit.add()
        for n in range(2):
            assert kit.call("POST", job, "draft", {"question": f"Q{n}?"}).status == 200
        blocked = kit.call("POST", job, "draft", {"question": "Q3?"})
        assert blocked.status == 429 and "drafts used today" in blocked.body["error"]["message"]

    def test_draft_failures_and_bad_input(self) -> None:
        kit = Kit(InMemoryKitStore())
        job = kit.add()

        class Broken:
            def draft(self, *, question: str, job: JobContext, current: str | None = None) -> str:
                raise DraftError("Anthropic is down")

        kit.drafter = Broken()  # type: ignore[assignment]
        failed = kit.call("POST", job, "draft", {"question": "Why?"})
        assert failed.status == 502 and "down" in failed.body["error"]["message"]
        assert kit.call("POST", job, "draft", {"question": "  "}).status == 400
        bad = kit.app.handle(
            Request(
                method="POST",
                path=f"/kit/{job}/draft",
                query={"t": tokens.sign(job, SECRET)},
                body="{nope",
            )
        )
        assert bad.status == 400


class TestYourAnswers:
    def test_your_answer_beats_a_draft_and_comes_back_on_reload(self, store: KitStore) -> None:
        kit = Kit(store)
        job = kit.add()
        kit.call("POST", job, "draft", {"question": "Why Salesforce?"})
        saved = kit.call(
            "POST", job, "answers", {"question": "Why Salesforce?", "answer": "My own words."}
        )
        assert saved.status == 200 and saved.body["answer"]["source"] == "user"
        again = kit.call("POST", job, "draft", {"question": "Why Salesforce?"})
        assert again.body["answer"]["text"] == "My own words."
        page = kit.call("GET", job).payload()
        assert "My own words." in page  # pasted questions come back under "Your other answers"

    def test_library_answers_are_reused_on_the_next_job(self, store: KitStore) -> None:
        kit = Kit(store)
        first = kit.add()
        kit.call(
            "POST",
            first,
            "answers",
            {
                "question": "Tell us about a project you are proud of",
                "answer": "I built X.",
                "save_to_library": True,
            },
        )
        second = kit.add(external_id="JR2", url="https://salesforce.wd12.myworkdayjobs.com/x_JR2")
        reused = kit.call(
            "POST", second, "draft", {"question": "Tell us about a project you are proud of."}
        )
        assert reused.body["answer"] == {
            **reused.body["answer"],
            "text": "I built X.",
            "source": "library",
        }
        assert kit.drafter.calls == []  # no draft spent

    def test_a_saved_choice_shows_as_the_option_to_pick(self, store: KitStore) -> None:
        transport = FakeTransport([ScriptedResponse.json(GREENHOUSE)])
        kit = Kit(store, http=build_client(transport))
        job = kit.add(
            company="Anthropic",
            url="https://job-boards.greenhouse.io/anthropic/jobs/5183051008",
            source="greenhouse",
            external_id="5183051008",
        )
        q = "Have you applied to the Fellows program before?"
        kit.call("POST", job, "answers", {"question": q, "answer": "No"})
        assert "Select: No" in kit.call("GET", job).payload()

    def test_too_long(self) -> None:
        kit = Kit(InMemoryKitStore())
        job = kit.add()
        assert (
            kit.call("POST", job, "answers", {"question": "Q", "answer": "x" * 20_001}).status
            == 400
        )


class TestResume:
    def test_redirects_to_a_private_link(self) -> None:
        kit = Kit(InMemoryKitStore())
        response = kit.call("GET", kit.add(), "resume")
        assert response.status == 302
        assert response.all_headers["Location"] == "https://s3.test/resume?sig=1"

    def test_no_resume(self) -> None:
        kit = Kit(InMemoryKitStore())
        kit.app.resume_url = lambda: None
        assert kit.call("GET", kit.add(), "resume").status == 404


class TestLambdaAndS3:
    def test_handler_translates_the_event(self) -> None:
        kit = Kit(InMemoryKitStore())
        job = kit.add()
        event = {
            "requestContext": {"http": {"method": "GET", "path": f"/kit/{job}"}},
            "queryStringParameters": {"t": tokens.sign(job, SECRET)},
        }
        result = handler(event, app=kit.app)
        assert result["statusCode"] == 200
        assert result["headers"]["Content-Type"].startswith("text/html")
        assert "<!doctype html>" in result["body"]
        resume = handler(
            {**event, "requestContext": {"http": {"method": "GET", "path": f"/kit/{job}/resume"}}},
            app=kit.app,
        )
        assert resume["statusCode"] == 302 and "body" not in resume

    def test_profile_and_resume_from_s3(self) -> None:
        import boto3
        from moto import mock_aws

        with mock_aws():
            s3 = boto3.client("s3", region_name="us-east-1")
            s3.create_bucket(Bucket="apply")
            files = S3Files("apply", client=s3)
            assert files.profile() == Profile()
            assert not files.has_resume() and files.resume_url() is None
            s3.put_object(
                Bucket="apply", Key="profile.json", Body=json.dumps({"first_name": "Alex"})
            )
            s3.put_object(Bucket="apply", Key="resume.pdf", Body=b"%PDF-1.7")
            assert files.profile().value("first_name") == "Alex"
            assert files.resume() == b"%PDF-1.7"
            url = files.resume_url()
            assert url is not None
            assert "resume.pdf" in url and "Signature" in url  # signed (SigV2 or SigV4)
