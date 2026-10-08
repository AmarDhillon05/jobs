"""Application questions: what a form asks, and which ones a profile answers.

Only Greenhouse publishes a job's real application form without a sign-in:
``GET boards-api.greenhouse.io/v1/boards/{token}/jobs/{id}?questions=true``
returns every question with its type, options and whether it's required. Lever
and Ashby don't; Workday keeps its questions behind each company's sign-in. For
those the kit uses its per-ATS walkthrough (:mod:`jobmonitor.apply.forms`) plus a
"paste a question" box.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from jobmonitor.apply.profile import match_question
from jobmonitor.errors import JobMonitorError
from jobmonitor.http import HttpClient
from jobmonitor.models.job import strip_html

GREENHOUSE_QUESTIONS_URL = (
    "https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job_id}?questions=true"
)

#: Question kinds. ``long`` is free text: the kind a draft is written for.
TEXT, LONG, SELECT, FILE = "text", "long", "select", "file"


@dataclass(frozen=True, slots=True)
class Question:
    label: str
    kind: str = TEXT
    required: bool = False
    options: tuple[str, ...] = ()
    #: The profile key that answers it, when one does (see ``match_question``).
    profile_key: str | None = None
    #: Shown under the question: an instruction rather than an answer.
    hint: str | None = None

    @property
    def draftable(self) -> bool:
        """Free text the profile can't answer: worth a draft from the resume."""
        return self.kind == LONG and self.profile_key is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "kind": self.kind,
            "required": self.required,
            "options": list(self.options),
            "profile_key": self.profile_key,
            "hint": self.hint,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Question:
        return cls(
            label=str(data["label"]),
            kind=str(data.get("kind") or TEXT),
            required=bool(data.get("required")),
            options=tuple(str(o) for o in data.get("options") or ()),
            profile_key=data.get("profile_key") or None,
            hint=data.get("hint") or None,
        )


@dataclass(slots=True)
class FetchedQuestions:
    questions: list[Question] = field(default_factory=list)
    description: str | None = None


_KINDS = {
    "input_text": TEXT,
    "textarea": LONG,
    "multi_value_single_select": SELECT,
    "multi_value_multi_select": SELECT,
    "input_file": FILE,
    "input_hidden": None,
}


def parse_greenhouse(payload: Mapping[str, Any]) -> FetchedQuestions:
    """Questions from a Greenhouse ``?questions=true`` job payload, in form order."""
    out = FetchedQuestions(description=strip_html(str(payload.get("content") or "")) or None)
    for raw in payload.get("questions") or ():
        if not isinstance(raw, Mapping):
            continue
        label = " ".join(str(raw.get("label") or "").split())
        fields = [f for f in raw.get("fields") or () if isinstance(f, Mapping)]
        if not label or not fields:
            continue
        kinds = [_KINDS.get(str(f.get("type"))) for f in fields]
        if FILE in kinds:
            kind = FILE  # "Resume/CV": a file input with a paste-text alternative
        elif SELECT in kinds:
            kind = SELECT
        elif LONG in kinds:
            kind = LONG
        elif TEXT in kinds:
            kind = TEXT
        else:
            continue
        options = tuple(
            str(value.get("label"))
            for f in fields
            for value in f.get("values") or ()
            if isinstance(value, Mapping) and value.get("label")
        )
        out.questions.append(
            Question(
                label=label,
                kind=kind,
                required=bool(raw.get("required")),
                options=options,
                profile_key=None if kind == FILE else match_question(label),
                hint="Attach the resume you downloaded above." if kind == FILE else None,
            )
        )
    return out


def fetch_greenhouse(client: HttpClient, board: str, job_id: str) -> FetchedQuestions:
    """The job's real application form. Raises ``JobMonitorError`` on HTTP failure."""
    url = GREENHOUSE_QUESTIONS_URL.format(board=board, job_id=job_id)
    payload = client.get_json(url)
    if not isinstance(payload, Mapping):
        raise JobMonitorError(f"{url} did not return a JSON object")
    return parse_greenhouse(payload)


__all__: Sequence[str] = (
    "FILE",
    "GREENHOUSE_QUESTIONS_URL",
    "LONG",
    "SELECT",
    "TEXT",
    "FetchedQuestions",
    "Question",
    "fetch_greenhouse",
    "parse_greenhouse",
)
