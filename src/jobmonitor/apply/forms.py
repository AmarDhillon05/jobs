"""The kit's walkthrough: an application form's pages, with the answers in order.

The user's pain on a phone (2026-10-08, applying to Salesforce on Workday) was
*too many pages and questions*. The kit can't remove the pages - Workday's form
sits behind each company's sign-in, and nothing here fills or submits a form for
the user (PRD §33) - so it mirrors them: one card per page, in the form's order,
every answer one tap from the clipboard and every dropdown shown as the option to
pick. Adding a question or an ATS is an edit to the data below.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date

from jobmonitor.apply.profile import DETAIL_FIELDS
from jobmonitor.apply.questions import LONG, SELECT, TEXT, Question

WORKDAY, GREENHOUSE, LEVER, ASHBY, OTHER = "workday", "greenhouse", "lever", "ashby", "other"


@dataclass(frozen=True, slots=True)
class Step:
    title: str
    questions: tuple[Question, ...] = ()
    tip: str | None = None
    #: Show the "paste any other question" box at the end of this step.
    paste_box: bool = False


@dataclass(frozen=True, slots=True)
class Walkthrough:
    ats: str
    steps: tuple[Step, ...] = field(default_factory=tuple)
    tip: str | None = None


def detect_ats(url: str, source: str | None = None) -> str:
    lowered = url.lower()
    if "myworkdayjobs.com" in lowered or "workday" in (source or ""):
        return WORKDAY
    if "greenhouse.io" in lowered or "gh_jid=" in lowered or source == "greenhouse":
        return GREENHOUSE
    if "lever.co" in lowered or source == "lever":
        return LEVER
    if "ashbyhq.com" in lowered or source == "ashby":
        return ASHBY
    return OTHER


def _q(label: str, key: str, kind: str = TEXT, hint: str | None = None) -> Question:
    return Question(label=label, kind=kind, profile_key=key, hint=hint)


def details_step(title: str = "Your details") -> Step:
    return Step(title=title, questions=tuple(_q(label, key) for key, label in DETAIL_FIELDS))


#: Asked on nearly every application, whatever the ATS.
COMMON_QUESTIONS: tuple[Question, ...] = (
    _q("Are you legally authorized to work in the US?", "work_authorization", SELECT),
    _q("Will you now require sponsorship?", "sponsorship_now", SELECT),
    _q("Will you now or in the future require sponsorship?", "sponsorship_future", SELECT),
    _q("Are you at least 18 years old?", "over_18", SELECT),
    _q("Expected graduation date", "graduation"),
    _q("Earliest start date / internship term", "earliest_start"),
    _q("How did you hear about us?", "how_did_you_hear", SELECT),
    _q("Have you previously worked here?", "previously_employed", SELECT),
    _q("Do you have relatives working here?", "relatives_at_company", SELECT),
    _q("Are you bound by a non-compete agreement?", "non_compete", SELECT),
)


def workday(today: date | None = None) -> Walkthrough:
    """Workday's application pages, in Workday's order."""
    today = today or date.today()
    return Walkthrough(
        ats=WORKDAY,
        tip=(
            "Applied to this company on Workday before? Choose **Use My Last Application** "
            "and most of this is filled in already."
        ),
        steps=(
            Step(
                "My Information",
                (
                    _q("How did you hear about us?", "how_did_you_hear", SELECT),
                    _q(
                        "Have you previously worked for this company?",
                        "previously_employed",
                        SELECT,
                    ),
                    _q("Country", "address.country", SELECT),
                    _q("First name", "first_name"),
                    _q("Last name", "last_name"),
                    _q("Address line 1", "address.line1"),
                    _q("City", "address.city"),
                    _q("State", "address.state", SELECT),
                    _q("Postal code", "address.postal_code"),
                    _q("Email", "email"),
                    _q("Phone device type", "phone_type", SELECT),
                    _q("Phone number", "phone"),
                ),
            ),
            Step(
                "My Experience",
                (
                    _q("School or university", "school"),
                    _q("Degree", "degree", SELECT),
                    _q("Field of study", "major"),
                    _q("Graduation (to)", "graduation"),
                    _q("Overall result (GPA)", "gpa"),
                    _q("Skills", "skills"),
                    _q("LinkedIn", "links.linkedin"),
                    _q("Website", "links.website"),
                    _q("GitHub", "links.github"),
                ),
                tip=(
                    "Tap **Upload** under Resume/CV and pick the resume you saved. Workday "
                    "fills in your work experience from it: check it against your resume, "
                    "then fix education with the answers below."
                ),
            ),
            Step(
                "Application Questions",
                COMMON_QUESTIONS[:6] + COMMON_QUESTIONS[8:],
                tip="Questions differ by company. Paste any that aren't here and tap Draft.",
                paste_box=True,
            ),
            Step(
                "Voluntary Disclosures",
                (
                    _q("Gender", "disclosures.gender", SELECT),
                    _q("Are you Hispanic or Latino?", "disclosures.hispanic", SELECT),
                    _q("Race / ethnicity", "disclosures.race", SELECT),
                    _q("Veteran status", "disclosures.veteran", SELECT),
                ),
                tip="These are optional. Then tick the box accepting the terms and conditions.",
            ),
            Step(
                "Self Identify",
                (
                    Question(label="Name", kind=TEXT, profile_key="__full_name__"),
                    Question(
                        label="Date",
                        kind=TEXT,
                        profile_key="__today__",
                        hint=f"Today: {today:%m/%d/%Y}",
                    ),
                    _q("Disability status", "disclosures.disability", SELECT),
                ),
                tip="The voluntary disability form (US federal contractors ask everyone).",
            ),
            Step(
                "Review",
                (),
                tip=(
                    "Scroll through once: check your graduation date, the resume file name "
                    "and any drafted answers. Then tap **Submit**."
                ),
            ),
        ),
    )


def generic(ats: str, form_questions: Sequence[Question] = ()) -> Walkthrough:
    """Greenhouse (with its fetched questions), Lever, Ashby and everything else.

    When the job's real form is known it *is* the walkthrough, in its own order:
    a separate details list would only repeat its name, email and link fields.
    """
    if form_questions:
        return Walkthrough(
            ats=ats,
            steps=(
                Step(
                    "Application form",
                    tuple(form_questions),
                    tip="This job's own questions, in the form's order.",
                    paste_box=True,
                ),
            ),
        )
    return Walkthrough(
        ats=ats,
        steps=(
            details_step(),
            Step(
                "Common questions",
                COMMON_QUESTIONS,
                tip="This form's own questions aren't published. Paste any others and tap Draft.",
                paste_box=True,
            ),
        ),
    )


def walkthrough_for(
    ats: str, form_questions: Sequence[Question] = (), today: date | None = None
) -> Walkthrough:
    if ats == WORKDAY:
        return workday(today)
    return generic(ats, form_questions)


__all__: Sequence[str] = (
    "ASHBY",
    "COMMON_QUESTIONS",
    "GREENHOUSE",
    "LEVER",
    "LONG",
    "OTHER",
    "WORKDAY",
    "Step",
    "Walkthrough",
    "details_step",
    "detect_ats",
    "generic",
    "walkthrough_for",
    "workday",
)
