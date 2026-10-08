"""The applicant's profile: the answers every application form asks for.

Kept as plain JSON written by the user (``apply/profile.example.json`` documents
every field) and read with dotted keys (``links.linkedin``), so adding a field is
an edit to the user's file, not a schema migration. The file lives in the user's
private S3 bucket in production and on disk locally; it is never committed.

:func:`match_question` maps an application form's question label to the profile
field that answers it ("LinkedIn Profile" -> ``links.linkedin``, "Will you now or
in the future require sponsorship?" -> ``sponsorship_future``). A question it
cannot place is left for the user, or for a draft if it asks for free text.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any


class ProfileError(ValueError):
    """The profile file is not usable."""


@dataclass(frozen=True, slots=True)
class Profile:
    data: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, text: str) -> Profile:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProfileError(f"profile is not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ProfileError("profile must be a JSON object")
        return cls(data)

    def value(self, key: str) -> str | None:
        """The value at a dotted key, as display text; ``None`` if unset."""
        node: Any = self.data
        for part in key.split("."):
            if not isinstance(node, Mapping) or part not in node:
                return None
            node = node[part]
        if node is None or node == "":
            return None
        if isinstance(node, list):
            return ", ".join(str(item) for item in node)
        if isinstance(node, bool):
            return "Yes" if node else "No"
        return str(node)

    @property
    def full_name(self) -> str:
        return " ".join(
            part for part in (self.value("first_name"), self.value("last_name")) if part
        )

    @property
    def library(self) -> Mapping[str, str]:
        """Starter answers the user wrote into the profile (``answers``)."""
        answers = self.data.get("answers")
        if not isinstance(answers, Mapping):
            return {}
        return {str(k): str(v) for k, v in answers.items() if v}

    def summary(self) -> str:
        """The facts a draft may rely on, as plain text for the drafting prompt."""
        lines = []
        for key, label in SUMMARY_FIELDS:
            value = self.value(key)
            if value:
                lines.append(f"{label}: {value}")
        return "\n".join(lines)


#: Profile fields shown on every kit, in this order: (key, label).
DETAIL_FIELDS: tuple[tuple[str, str], ...] = (
    ("first_name", "First name"),
    ("last_name", "Last name"),
    ("preferred_name", "Preferred name"),
    ("email", "Email"),
    ("phone", "Phone"),
    ("address.line1", "Address"),
    ("address.city", "City"),
    ("address.state", "State"),
    ("address.postal_code", "ZIP code"),
    ("address.country", "Country"),
    ("school", "School"),
    ("degree", "Degree"),
    ("major", "Major / field of study"),
    ("minor", "Minor"),
    ("graduation", "Graduation date"),
    ("gpa", "GPA"),
    ("links.linkedin", "LinkedIn"),
    ("links.github", "GitHub"),
    ("links.website", "Website / portfolio"),
    ("links.scholar", "Publications"),
    ("skills", "Skills"),
)

SUMMARY_FIELDS: tuple[tuple[str, str], ...] = (
    *DETAIL_FIELDS[:3],
    ("school", "School"),
    ("degree", "Degree"),
    ("major", "Major"),
    ("minor", "Minor"),
    ("graduation", "Graduation"),
    ("gpa", "GPA"),
    ("skills", "Skills"),
    ("work_authorization", "Authorized to work in the US"),
    ("sponsorship_future", "Will require sponsorship"),
    ("earliest_start", "Earliest start"),
)

#: Question label patterns -> the profile key that answers them. First match wins,
#: so the specific patterns come before the general ones.
QUESTION_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bfirst name\b|\bgiven name\b", "first_name"),
    (r"\blast name\b|\bsurname\b|\bfamily name\b", "last_name"),
    (r"\bpreferred (first )?name\b", "preferred_name"),
    (r"\be-?mail\b", "email"),
    (r"\bphone\b|\bmobile\b", "phone"),
    (r"\blinked ?in\b", "links.linkedin"),
    (r"\bgit ?hub\b", "links.github"),
    (r"\bpublications?\b|\bgoogle scholar\b", "links.scholar"),
    (r"\bwebsite\b|\bportfolio\b|\bpersonal (site|url)\b", "links.website"),
    (r"\b(now or in the future|future)\b.*\bsponsor", "sponsorship_future"),
    (r"\bsponsor", "sponsorship_future"),
    (r"\b(legally )?(authori[sz]ed|eligible) to work\b", "work_authorization"),
    (r"\b18\b.*\b(years|older|age)\b|\bage of 18\b", "over_18"),
    (r"\bhow did you (hear|learn|find)\b|\bsource\b.*\bapplication\b", "how_did_you_hear"),
    (r"\b(previously|ever) (been )?(worked|employed)\b|\bformer employee\b", "previously_employed"),
    (r"\brelatives?\b|\bfamily members?\b.*\bemploy", "relatives_at_company"),
    (r"\bnon-?compete\b|\bnon-?solicitation\b", "non_compete"),
    (r"\b(earliest|available) (start|to start)\b|\bstart date\b", "earliest_start"),
    (r"\bgraduat", "graduation"),
    (r"\bgpa\b|\bgrade point\b", "gpa"),
    (r"\bschool\b|\buniversity\b|\bcollege\b|\binstitution\b", "school"),
    (r"\bdegree\b", "degree"),
    (r"\bmajor\b|\bfield of study\b|\bdiscipline\b", "major"),
    (r"\bcity\b", "address.city"),
    (r"\bzip\b|\bpostal\b", "address.postal_code"),
    (r"\b(current )?location\b|\bwhere are you (based|located)\b", "address.city"),
)
_COMPILED = tuple((re.compile(pattern, re.I), key) for pattern, key in QUESTION_PATTERNS)


def match_question(label: str) -> str | None:
    """The profile key that answers a form question, if one clearly does."""
    for pattern, key in _COMPILED:
        if pattern.search(label):
            return key
    return None


def normalize_question(label: str) -> str:
    """A question's identity across companies: case, punctuation and the company
    name don't matter ("Why Anthropic?" and "Why Stripe?" are different questions,
    but "Why do you want this internship?" is the same everywhere)."""
    return re.sub(r"[^a-z0-9]+", " ", label.casefold()).strip()


__all__: Sequence[str] = (
    "DETAIL_FIELDS",
    "QUESTION_PATTERNS",
    "Profile",
    "ProfileError",
    "match_question",
    "normalize_question",
)
