"""Student programs posted on job boards.

Some companies post their early-insight programs, fellowships and hackathons as
ordinary job postings ("LINK 2027: Software Development Intensive Program",
"Discovery Program: Capital Markets", "Perplexity Research Fellowship"). With no
internship signal the relevance scorer would drop them, so they are recognised
here first and tagged ``employment_type="Program"``; the filter then keeps them as
events (:meth:`JobFilter.evaluate`).

Precision matters more than recall: a board lists far more jobs than programs, and
"Program Manager" or "Insights Analyst" must never ring the phone as a program.
So a title must name a program explicitly, must not read as a role, and must not
be an internship (those already alert as internships - "Summer Internship
Program" stays one).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Final

from jobmonitor.models.job import PROGRAM_TYPE, Job, is_event_kind

#: Phrases that name a program. Word-bounded, case-insensitive.
PROGRAM_PATTERNS: Final[tuple[str, ...]] = (
    r"fellowship",
    r"fellows program(?:me)?",
    r"insight (?:day|days|week|weeks|program|programme|series|event)s?",
    r"early insights?",
    r"discovery (?:day|days|program|programme|week)s?",
    r"discover \w+",  # "Discover Citadel", "Discover DRW"
    r"intensive program(?:me)?",
    r"hackathon",
    r"case competition",
    r"trading (?:competition|challenge|invitational)",
    r"externship",
    r"(?:sophomore|first[- ]year|freshman|second[- ]year) (?:program|programme|summit|day)s?",
    r"apprentice programme",
    r"apprenticeship",
    r"academy",
    r"summit",
    r"coffee chats?",
    r"info(?:rmation)? sessions?",
    r"open house",
)

#: Words that make a title a job (or an internship) rather than a program.
ROLE_PATTERNS: Final[tuple[str, ...]] = (
    r"intern",
    r"internship",
    r"co-?op",
    r"program(?:me)? manager",
    r"program(?:me)? management",
    r"product manager",
    r"manager",
    r"director",
    r"head of",
    r"lead",
    r"coordinator",
    r"specialist",
    r"recruiter",
    r"recruiting",
    r"teaching assistant",
    r"instructor",
    r"dean",
    r"marketer",
    r"marketing",
    r"analyst",
    r"engineer",
    r"developer",
    r"scientist",
    r"researcher",
    r"associate",
    r"administrator",
    r"partner",
)


def _compile(patterns: Iterable[str]) -> re.Pattern[str]:
    return re.compile(r"\b(?:" + "|".join(patterns) + r")\b", re.IGNORECASE)


_PROGRAM_RE = _compile(PROGRAM_PATTERNS)
_ROLE_RE = _compile(ROLE_PATTERNS)
#: A role word inside the program's own name ("Software *Development* Intensive
#: Program", "*Investment Analyst* ...") is fine when the title says it is a
#: program in so many words: "... Fellowship", "... Program".
_EXPLICIT_RE = _compile(
    (
        r"fellowship",
        r"fellows program(?:me)?",
        r"discovery program(?:me)?",
        r"intensive program(?:me)?",
        r"insight program(?:me)?",
        r"hackathon",
    )
)
#: Internship words always win, explicit or not.
_INTERNSHIP_RE = _compile((r"intern", r"internship", r"co-?op"))


def is_program_title(title: str) -> bool:
    """Whether a job-board title is a student program rather than a role."""
    if not _PROGRAM_RE.search(title):
        return False
    if _INTERNSHIP_RE.search(title):
        return False
    if _EXPLICIT_RE.search(title):
        # "Program Manager, Fellowship" is still a role.
        return not re.search(
            r"\b(?:program(?:me)? manager|manager|director|coordinator|recruiter|"
            r"teaching assistant|instructor|dean|lead)\b",
            title,
            re.IGNORECASE,
        )
    return not _ROLE_RE.search(title)


def tag_programs(jobs: Iterable[Job]) -> list[Job]:
    """Tag the programs among a board's postings; everything else is unchanged."""
    tagged: list[Job] = []
    for job in jobs:
        if not is_event_kind(job.employment_type) and is_program_title(job.title):
            job = job.with_fields(employment_type=PROGRAM_TYPE)
        tagged.append(job)
    return tagged


__all__: Sequence[str] = (
    "PROGRAM_PATTERNS",
    "ROLE_PATTERNS",
    "is_program_title",
    "tag_programs",
)
