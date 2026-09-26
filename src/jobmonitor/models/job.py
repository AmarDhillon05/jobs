"""The normalized job posting.

Every adapter, whatever shape its source speaks, produces this one type. The
constructor normalizes whitespace and validates the fields the rest of the
pipeline depends on, raising :class:`InvalidJobError` for a record it cannot
use - which adapters catch per record so one bad posting never discards the good
ones in the same response (PRD §7).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from jobmonitor.errors import InvalidJobError

_WHITESPACE_RE = re.compile(r"\s+")
#: Common HTML entities that leak through ATS description/title fields.
_ENTITIES: Mapping[str, str] = {
    "&amp;": "&",
    "&lt;": "<",
    "&gt;": ">",
    "&quot;": '"',
    "&#39;": "'",
    "&apos;": "'",
    "&nbsp;": " ",
    "&ndash;": "-",
    "&mdash;": "-",
}
_TAG_RE = re.compile(r"<[^>]+>")

MAX_DESCRIPTION_CHARS = 8000
"""Descriptions are truncated before storage: DynamoDB items are capped at 400KB
and the client only ever shows a preview."""


def clean_text(value: str | None) -> str | None:
    """Collapse whitespace and decode the handful of entities ATSes emit."""
    if value is None:
        return None
    text = value
    for entity, replacement in _ENTITIES.items():
        text = text.replace(entity, replacement)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text or None


def strip_html(value: str | None) -> str | None:
    """Turn an HTML description into plain text suitable for an email/preview."""
    if value is None:
        return None
    # Keep paragraph boundaries as spaces rather than gluing words together.
    text = re.sub(r"<(?:br|/p|/li|/div|/h[1-6])\s*/?>", " ", value, flags=re.IGNORECASE)
    text = _TAG_RE.sub(" ", text)
    return clean_text(text)


def _require(value: str | None, field: str, company: str) -> str:
    cleaned = clean_text(value)
    if not cleaned:
        raise InvalidJobError(f"{company or '<unknown company>'}: missing required field {field!r}")
    return cleaned


def _validate_url(url: str, company: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        raise InvalidJobError(f"{company}: job url must be http(s), got {url!r}")
    if not parts.netloc or "." not in parts.netloc:
        raise InvalidJobError(f"{company}: job url has no usable host: {url!r}")
    if " " in url:
        raise InvalidJobError(f"{company}: job url contains a space: {url!r}")
    return url


def _coerce_datetime(value: Any, company: str) -> datetime | None:
    """Accept what real ATS payloads actually contain, or nothing at all."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        # Epoch seconds, or milliseconds (Ashby/Workday both emit ms in places).
        seconds = float(value)
        if seconds > 1e11:
            seconds /= 1000.0
        try:
            return datetime.fromtimestamp(seconds, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        candidate = text.replace("Z", "+00:00") if text.endswith("Z") else text
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%B %d, %Y", "%b %d, %Y", "%d %b %Y"):
                try:
                    parsed = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
            else:
                # An unreadable date is not a reason to discard a job: first_seen
                # is what detection actually relies on (PRD §9).
                return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


@dataclass(frozen=True, slots=True)
class Job:
    """One normalized posting. Field set is fixed by PRD §6."""

    company: str
    title: str
    url: str
    source: str
    location: str | None = None
    external_id: str | None = None
    date_posted: datetime | None = None
    description: str | None = None
    employment_type: str | None = None

    def __post_init__(self) -> None:
        company = _require(self.company, "company", self.company)
        object.__setattr__(self, "company", company)
        object.__setattr__(self, "title", _require(self.title, "title", company))
        raw_url = clean_text(self.url)
        if not raw_url:
            raise InvalidJobError(f"{company}: missing required field 'url'")
        object.__setattr__(self, "url", _validate_url(raw_url, company))
        object.__setattr__(self, "source", _require(self.source, "source", company))
        object.__setattr__(self, "location", clean_text(self.location))
        external_id = clean_text(self.external_id)
        object.__setattr__(self, "external_id", external_id)
        object.__setattr__(self, "employment_type", clean_text(self.employment_type))
        object.__setattr__(self, "date_posted", _coerce_datetime(self.date_posted, company))

        description = strip_html(self.description)
        if description and len(description) > MAX_DESCRIPTION_CHARS:
            description = description[:MAX_DESCRIPTION_CHARS].rstrip() + "..."
        object.__setattr__(self, "description", description)

    # ------------------------------------------------------------------ helpers
    @property
    def has_stable_external_id(self) -> bool:
        return bool(self.external_id)

    def description_preview(self, limit: int = 280) -> str:
        if not self.description:
            return ""
        if len(self.description) <= limit:
            return self.description
        return self.description[:limit].rstrip() + "..."

    def with_fields(self, **changes: Any) -> Job:
        return replace(self, **changes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "company": self.company,
            "title": self.title,
            "url": self.url,
            "source": self.source,
            "location": self.location,
            "external_id": self.external_id,
            "date_posted": self.date_posted.isoformat() if self.date_posted else None,
            "description": self.description,
            "employment_type": self.employment_type,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Job:
        return cls(
            company=str(data.get("company", "")),
            title=str(data.get("title", "")),
            url=str(data.get("url", "")),
            source=str(data.get("source", "")),
            location=data.get("location"),
            external_id=data.get("external_id"),
            date_posted=data.get("date_posted"),
            description=data.get("description"),
            employment_type=data.get("employment_type"),
        )


__all__: Sequence[str] = (
    "MAX_DESCRIPTION_CHARS",
    "Job",
    "clean_text",
    "strip_html",
)
