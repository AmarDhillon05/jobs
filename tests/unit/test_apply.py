"""The Apply kit's pure parts: tokens, profile, question matching, forms, drafting.

The Greenhouse fixture is a live capture (2026-10-08) of a real application form:
Anthropic's Fellows Program, ML Systems & RL (``?questions=true``).
"""

from __future__ import annotations

import base64
import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from tests.support.http import load_fixture

from jobmonitor.apply import forms, tokens
from jobmonitor.apply.drafter import (
    FALLBACK_BETA,
    MODEL,
    ClaudeDrafter,
    DraftError,
    DraftRefused,
    JobContext,
    answer_text,
    build_request,
)
from jobmonitor.apply.profile import Profile, ProfileError, match_question, normalize_question
from jobmonitor.apply.questions import FILE, LONG, SELECT, TEXT, parse_greenhouse

EXAMPLE_PROFILE = Path(__file__).resolve().parents[2] / "apply" / "profile.example.json"
GREENHOUSE = load_fixture("apply", "greenhouse_questions_anthropic_fellows.json")
JOB = JobContext(company="Anthropic", title="Fellows Program", location="SF", description="RL")


class TestTokens:
    def test_round_trip(self) -> None:
        token = tokens.sign("salesforce:JR1", "s3cret")
        assert tokens.verify("salesforce:JR1", token, "s3cret")
        assert len(token) == 22 and "=" not in token

    @pytest.mark.parametrize(
        ("job_id", "token", "secret"),
        [
            ("salesforce:JR2", None, "s3cret"),  # filled below: a token for another job
            ("salesforce:JR1", "", "s3cret"),
            ("salesforce:JR1", None, None),
            ("salesforce:JR1", "x" * 22, "s3cret"),
        ],
    )
    def test_rejections(self, job_id: str, token: str | None, secret: str | None) -> None:
        if token is None and secret:
            token = tokens.sign("salesforce:JR1", secret)
        assert not tokens.verify(job_id, token, secret)

    def test_another_secret_does_not_verify(self) -> None:
        assert not tokens.verify("a", tokens.sign("a", "one"), "two")

    def test_an_empty_secret_cannot_sign(self) -> None:
        with pytest.raises(ValueError):
            tokens.sign("a", "")


class TestProfile:
    def profile(self) -> Profile:
        return Profile.from_json(EXAMPLE_PROFILE.read_text())

    def test_dotted_values(self) -> None:
        p = self.profile()
        assert p.value("links.github") == "https://github.com/alex-rivera-example"
        assert p.value("address.city") == "Berkeley"
        assert p.value("skills").startswith("Python, Java")  # type: ignore[union-attr]
        assert p.value("minor") is None  # empty string is unset
        assert p.value("nope.deeper") is None
        assert p.full_name == "Alex Rivera"

    def test_booleans_read_as_yes_no(self) -> None:
        assert Profile({"over_18": True}).value("over_18") == "Yes"

    def test_summary_is_the_facts_for_a_draft(self) -> None:
        summary = self.profile().summary()
        assert "School: University of California, Berkeley" in summary
        assert "Graduation: May 2028" in summary
        assert "@" not in summary  # contact details never go to the model

    def test_bad_json(self) -> None:
        with pytest.raises(ProfileError):
            Profile.from_json("{nope")
        with pytest.raises(ProfileError):
            Profile.from_json("[1, 2]")

    @pytest.mark.parametrize(
        ("label", "key"),
        [
            ("First Name", "first_name"),
            ("LinkedIn Profile", "links.linkedin"),
            ("GitHub URL", "links.github"),
            ("Publications (e.g. Google Scholar) URL", "links.scholar"),
            ("Website", "links.website"),
            ("Will you now or in the future require sponsorship for employment visa status?",
             "sponsorship_future"),
            ("Are you legally authorized to work in the United States?", "work_authorization"),
            ("Are you at least 18 years of age?", "over_18"),
            ("How did you hear about this opportunity?", "how_did_you_hear"),
            ("Have you previously been employed by Salesforce?", "previously_employed"),
            ("What is your expected graduation date?", "graduation"),
            ("Current GPA", "gpa"),
            ("Which university do you attend?", "school"),
            ("Why Anthropic?", None),
            ("Have you applied to the Fellows program before?", None),
        ],
    )  # fmt: skip
    def test_question_matching(self, label: str, key: str | None) -> None:
        assert match_question(label) == key

    def test_normalized_questions_ignore_case_and_punctuation(self) -> None:
        assert normalize_question("Why do you want THIS internship?") == normalize_question(
            "why do you want this internship"
        )


class TestGreenhouseQuestions:
    def test_the_real_form_in_order(self) -> None:
        parsed = parse_greenhouse(GREENHOUSE)
        labels = [q.label for q in parsed.questions]
        assert labels[:5] == ["First Name", "Last Name", "Email", "Phone", "Resume/CV"]
        assert "Why Anthropic?" in labels
        by_label = {q.label: q for q in parsed.questions}
        assert by_label["First Name"].profile_key == "first_name"
        assert by_label["First Name"].required
        assert by_label["Resume/CV"].kind == FILE
        assert by_label["Have you applied to the Fellows program before?"].kind == SELECT
        assert by_label["Have you applied to the Fellows program before?"].options == ("Yes", "No")
        why = by_label["Why Anthropic?"]
        assert (why.kind, why.required, why.draftable) == (LONG, True, True)
        assert by_label["LinkedIn"].kind == TEXT and not by_label["LinkedIn"].draftable
        assert parsed.description and "Anthropic" in parsed.description

    def test_round_trip_for_the_cache(self) -> None:
        from jobmonitor.apply.questions import Question

        for q in parse_greenhouse(GREENHOUSE).questions:
            assert Question.from_dict(json.loads(json.dumps(q.to_dict()))) == q

    def test_junk_is_skipped(self) -> None:
        payload = {
            "questions": [
                {"label": "", "fields": []},
                "nope",
                {"label": "X", "fields": [{"type": "input_hidden"}]},
            ]
        }
        assert parse_greenhouse(payload).questions == []


class TestForms:
    @pytest.mark.parametrize(
        ("url", "source", "ats"),
        [
            ("https://salesforce.wd12.myworkdayjobs.com/en-US/External/job/x", "workday", forms.WORKDAY),
            ("https://job-boards.greenhouse.io/anthropic/jobs/1", "greenhouse", forms.GREENHOUSE),
            ("https://stripe.com/jobs/listing/x/1?gh_jid=1", "greenhouse", forms.GREENHOUSE),
            ("https://jobs.lever.co/palantir/abc", "lever", forms.LEVER),
            ("https://jobs.ashbyhq.com/openai/abc", "ashby", forms.ASHBY),
            ("https://careers.example.com/1", "custom", forms.OTHER),
        ],
    )  # fmt: skip
    def test_detect(self, url: str, source: str, ats: str) -> None:
        assert forms.detect_ats(url, source) == ats

    def test_workday_mirrors_its_pages(self) -> None:
        walk = forms.workday(date(2026, 10, 8))
        assert [s.title for s in walk.steps] == [
            "My Information",
            "My Experience",
            "Application Questions",
            "Voluntary Disclosures",
            "Self Identify",
            "Review",
        ]
        assert "Use My Last Application" in (walk.tip or "")
        questions = walk.steps[2]
        assert questions.paste_box
        assert any(q.profile_key == "sponsorship_future" for q in questions.questions)
        assert "10/08/2026" in (walk.steps[4].questions[1].hint or "")

    def test_a_known_form_is_the_whole_walkthrough(self) -> None:
        parsed = parse_greenhouse(GREENHOUSE).questions
        walk = forms.walkthrough_for(forms.GREENHOUSE, parsed)
        assert len(walk.steps) == 1 and walk.steps[0].questions == tuple(parsed)

    def test_an_unknown_form_gets_details_and_common_questions(self) -> None:
        walk = forms.walkthrough_for(forms.LEVER)
        assert [s.title for s in walk.steps] == ["Your details", "Common questions"]
        assert walk.steps[1].paste_box


class FakeMessages:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.requests: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def fake_client(response: Any) -> Any:
    return SimpleNamespace(beta=SimpleNamespace(messages=FakeMessages(response)))


def reply(text: str, stop_reason: str = "end_turn") -> Any:
    return SimpleNamespace(
        stop_reason=stop_reason,
        stop_details=SimpleNamespace(category="cyber") if stop_reason == "refusal" else None,
        content=[
            SimpleNamespace(type="thinking", thinking=""),
            SimpleNamespace(type="text", text=text),
        ],
        usage=SimpleNamespace(input_tokens=10, cache_read_input_tokens=0, output_tokens=5),
    )


class TestDrafter:
    def test_request_shape(self) -> None:
        request = build_request(
            question="Why Anthropic?",
            job=JOB,
            resume_pdf=b"%PDF-1.7 fake",
            profile_summary="School: Berkeley",
            current="I like safety",
        )
        assert request["model"] == MODEL == "claude-opus-5-5"
        assert request["fallbacks"] == "default" and request["betas"] == [FALLBACK_BETA]
        assert request["output_config"] == {"effort": "low"}
        assert "thinking" not in request  # always on for this model; not configurable
        document, text = request["messages"][0]["content"]
        assert document["type"] == "document"
        assert document["source"]["media_type"] == "application/pdf"
        assert base64.b64decode(document["source"]["data"]) == b"%PDF-1.7 fake"
        assert document["cache_control"] == {"type": "ephemeral"}
        assert "Why Anthropic?" in text["text"] and "I like safety" in text["text"]
        assert "School: Berkeley" in text["text"]
        assert "Never invent experience" in request["system"]

    def test_drafts_through_the_client(self) -> None:
        client = fake_client(reply("  I want to work on reliable ML systems.  "))
        drafter = ClaudeDrafter(api_key="k", resume_pdf=b"%PDF", client=client)
        assert drafter.draft(question="Why?", job=JOB) == "I want to work on reliable ML systems."
        assert client.beta.messages.requests[0]["model"] == MODEL

    def test_refusal(self) -> None:
        with pytest.raises(DraftRefused, match="cyber"):
            answer_text(reply("", stop_reason="refusal"))

    def test_empty_reply_and_api_errors(self) -> None:
        with pytest.raises(DraftError):
            answer_text(reply("   "))
        drafter = ClaudeDrafter(
            api_key="k", resume_pdf=b"%PDF", client=fake_client(RuntimeError("boom"))
        )
        with pytest.raises(DraftError, match="boom"):
            drafter.draft(question="Why?", job=JOB)

    def test_no_resume_no_drafter(self) -> None:
        with pytest.raises(DraftError, match="resume"):
            ClaudeDrafter(api_key="k", resume_pdf=b"", client=fake_client(reply("x")))
