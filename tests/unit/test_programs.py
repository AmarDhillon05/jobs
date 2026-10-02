"""Programs on job boards, and how events are scored (Level 1).

Every title below is a real posting seen on the company's board on 2026-10-01.
"""

from __future__ import annotations

import pytest
from tests.conftest import make_company

from jobmonitor.config import FilterSettings
from jobmonitor.filtering import JobFilter
from jobmonitor.filtering.programs import is_program_title, tag_programs
from jobmonitor.models.company import Priority
from jobmonitor.models.job import (
    EVENT_TYPE,
    INDUSTRY_EVENT_TYPE,
    PROGRAM_TYPE,
    Job,
    is_event_kind,
    posting_kind,
)

PROGRAMS = [
    "LINK 2027: Software Development Intensive Program",  # Five Rings
    "Perplexity Research Fellowship",
    "AI Research Fellowship, (Summer and Fall 2026)",  # DoorDash
    "Neurodivergent Fellowship",  # Palantir
    "Anthropic Fellows Program, ML Systems & Reinforcement Learning",
    "Discovery Program: Capital Markets (On-site)",  # SIG
    "Discovery Program: Trading System Engineer (On-site)",  # SIG
    "Hong Kong PhD Discovery Program: April 2027",  # SIG
    "National Security Hackathon 2026 - General Interest",  # Scale AI
    "2026 Point72 Academy National Case Competition - US",
    "2027 Point72 Academy Spring Insight Programme \u2013 UK",
    "Point72 Academy Coffee Chats — Class of 2029 (US)",
    "2027 US Chess Academy Interest Form",  # IMC
    "2027 | EMEA | London | Engineering | Apprentice Programme",  # Goldman
    "Discover Citadel",
    "Sophomore Summit",
    "Virtual Info Session: Software Engineering",
]

NOT_PROGRAMS = [
    # Roles that mention a program.
    "Teaching Assistant, Academy of Math and Programming (AMP)",  # Jane Street
    "Dean of Students, Academy of Math and Programming (AMP)",
    "AI Instructor, Point72 Academy",
    "Point72 Academy 2026 Investment Analyst Program for Experienced Professionals - UK",
    "Point72 Academy Investment Analyst Program for Upcoming Graduates (2027 \u2013 HK)",
    "Technical Program Manager, Infrastructure",
    "Program Manager, Fellowship Programs",
    "Insights Analyst",
    "Event Marketer",
    "Senior Software Engineer, Events Platform",
    "Head of Developer Relations - Summit Series",
    # Internships stay internships.
    "2027 Point72 Academy Investment Analyst Summer Internship Program - Japan",
    "2027 Intern - Adobe Sales Academy BDR",
    "Software Engineer Intern - Hackathon Team",
    # Plain jobs.
    "Software Engineer Intern",
    "Backend Engineer",
    "Recruiting Coordinator, Campus Programs",
]


@pytest.mark.parametrize("title", PROGRAMS)
def test_programs_are_recognised(title: str) -> None:
    assert is_program_title(title)


@pytest.mark.parametrize("title", NOT_PROGRAMS)
def test_roles_and_internships_are_not_programs(title: str) -> None:
    assert not is_program_title(title)


def job(title: str, employment_type: str | None = None, **kwargs: object) -> Job:
    payload: dict[str, object] = {
        "company": "TestCo",
        "title": title,
        "url": "https://job-boards.greenhouse.io/testco/jobs/1",
        "source": "greenhouse",
        "external_id": "1",
        "employment_type": employment_type,
    }
    payload.update(kwargs)
    return Job(**payload)  # type: ignore[arg-type]


class TestTagging:
    def test_only_programs_are_tagged(self) -> None:
        tagged = tag_programs(
            [job("Perplexity Research Fellowship"), job("Software Engineer Intern")]
        )
        assert [j.employment_type for j in tagged] == [PROGRAM_TYPE, None]

    def test_an_existing_event_tag_is_kept(self) -> None:
        tagged = tag_programs([job("Hackathon", employment_type=EVENT_TYPE)])
        assert tagged[0].employment_type == EVENT_TYPE

    def test_kinds(self) -> None:
        assert posting_kind(None) == "internship"
        assert posting_kind("Full-time") == "internship"
        assert posting_kind("event") == "event"
        assert posting_kind(INDUSTRY_EVENT_TYPE) == "industry_event"
        assert posting_kind(PROGRAM_TYPE) == "program"
        assert is_event_kind(PROGRAM_TYPE) and not is_event_kind("Intern")


class TestEventScoring:
    def filter(self) -> JobFilter:
        return JobFilter(FilterSettings())

    def test_recruiting_events_and_programs_score_80_and_alert_immediately(self) -> None:
        company = make_company(priority=Priority.EXPERIMENTAL)
        for employment_type in (EVENT_TYPE, PROGRAM_TYPE):
            decision = self.filter().evaluate(job("Info Session", employment_type), company)
            assert (decision.score, decision.keep, decision.notify, decision.immediate) == (
                80,
                True,
                True,
                True,
            )

    def test_industry_events_score_60_and_follow_the_company_tier(self) -> None:
        low = make_company(priority=Priority.EXPERIMENTAL)
        high = make_company(priority=Priority.HIGH)
        decision = self.filter().evaluate(job("Data + AI Summit", INDUSTRY_EVENT_TYPE), low)
        assert (decision.score, decision.keep, decision.notify, decision.immediate) == (
            60,
            True,
            True,
            False,
        )
        assert self.filter().evaluate(job("Summit", INDUSTRY_EVENT_TYPE), high).immediate

    def test_events_are_kept_whatever_their_words(self) -> None:
        # "Marketing" would sink an internship; an event is never dropped by type.
        decision = self.filter().evaluate(job("Marketing Leaders Dinner", INDUSTRY_EVENT_TYPE))
        assert decision.keep
        assert "always kept" in decision.reason

    def test_a_program_title_without_the_tag_would_be_dropped(self) -> None:
        # Why tag_programs exists: untagged, the internship scorer drops it.
        decision = self.filter().evaluate(job("Perplexity Research Fellowship"))
        assert not decision.keep
