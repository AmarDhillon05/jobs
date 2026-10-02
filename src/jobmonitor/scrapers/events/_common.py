"""Helpers shared by the event adapters.

Rules every event adapter follows (ARCHITECTURE.md, "Events"):

* ``date_posted`` is always ``None``. An event's date is *when it happens*, so it
  goes in the title ("Bridge · Nov 14, 2026") and never into the posting-age
  window. ``first_seen`` drives detection, as for an undated job.
* An event whose date is known and already past is skipped at parse time.
* External ids are prefixed ``event:`` so they can never collide with a job id.
"""

from __future__ import annotations

import html
import re
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta

from jobmonitor.models.job import clean_text

#: How long after its (start) date an event still counts as current. Covers
#: multi-day conferences whose end date the listing does not give.
PAST_GRACE = timedelta(days=2)

_MONTHS = {
    name: index
    for index, names in enumerate(
        (
            ("jan", "january"),
            ("feb", "february"),
            ("mar", "march"),
            ("apr", "april"),
            ("may",),
            ("jun", "june"),
            ("jul", "july"),
            ("aug", "august"),
            ("sep", "sept", "september"),
            ("oct", "october"),
            ("nov", "november"),
            ("dec", "december"),
        ),
        start=1,
    )
    for name in names
}
_MONTH = (
    r"(?P<month>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
    r"sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?"
)
_DASH = r"\s*(?:-|\u2013|\u2014|to|&)\s*"

#: The date formats event listings use, most specific first. Each names
#: ``month``, ``day`` and optionally ``year`` (and ``end`` for ranges).
_DATE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        # 2026-11-04
        r"\b(?P<year>20\d\d)-(?P<nmonth>\d\d)-(?P<day>\d\d)\b",
        # November 4, 2026 / Nov 4-6, 2026 / Nov. 4 - Nov. 6, 2026 / Nov 4th 2026
        # (also "Sep . 30 - Oct 01 , 2026", as one site writes it)
        rf"\b{_MONTH}\s*\.?\s*(?P<day>\d{{1,2}})(?:st|nd|rd|th)?"
        rf"(?:{_DASH}(?:[a-z]{{3,9}}\s*\.?\s*)?(?P<end>\d{{1,2}})(?:st|nd|rd|th)?)?"
        r"\s*,?\s+(?P<year>20\d\d)\b",
        # 4 November 2026 / 4-6 November 2026
        rf"\b(?P<day>\d{{1,2}})(?:st|nd|rd|th)?(?:{_DASH}(?P<end>\d{{1,2}}))?\s+{_MONTH},?\s+(?P<year>20\d\d)\b",
        # 11/04/2026 (US order)
        r"\b(?P<nmonth>\d{1,2})/(?P<day>\d{1,2})/(?P<year>20\d\d)\b",
        # Nov 4 / November 4th (no year: the next such date)
        rf"\b{_MONTH}\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\b(?![:\d])",
    )
)


def text_of(fragment: str) -> str:
    """Visible text of an HTML fragment, whitespace collapsed."""
    stripped = re.sub(r"<(script|style|noscript|svg)\b.*?</\1>", " ", fragment, flags=re.S | re.I)
    return clean_text(html.unescape(re.sub(r"<[^>]+>", " ", stripped))) or ""


def find_date(value: str, *, today: date) -> tuple[date, str] | None:
    """The first date in ``value`` and the text it was read from.

    A date without a year is taken as its occurrence nearest ``today``: listings
    write "Oct 14" for this year's event and "Jan 30" for next year's.
    """
    best: tuple[int, date, str] | None = None
    for pattern in _DATE_PATTERNS:
        for match in pattern.finditer(value):
            parsed = _to_date(match, today=today)
            if parsed is None:
                continue
            if best is None or match.start() < best[0]:
                best = (match.start(), parsed, match.group(0))
            break
    return (best[1], best[2]) if best else None


def _to_date(match: re.Match[str], *, today: date) -> date | None:
    groups = match.groupdict()
    month = (
        int(groups["nmonth"])
        if groups.get("nmonth")
        else _MONTHS.get(str(groups.get("month") or "").lower().rstrip("."), 0)
    )
    try:
        # A range ("Nov 30 - Dec 2") is dated by its start; PAST_GRACE covers the rest.
        day = int(groups["day"])
        if groups.get("year"):
            return date(int(groups["year"]), month, day)
        candidates = [date(today.year + offset, month, day) for offset in (-1, 0, 1)]
    except (TypeError, ValueError):
        return None
    # The occurrence nearest today: on Oct 2, "Jan 30" is next January and
    # "Sep 8" is last month (so already past), not next September.
    return min(candidates, key=lambda candidate: abs((candidate - today).days))


def is_past(when: date | datetime | None, *, now: datetime) -> bool:
    if when is None:
        return False
    day = when.date() if isinstance(when, datetime) else when
    return day < (now.astimezone(UTC).date() - PAST_GRACE)


def format_day(when: date) -> str:
    return f"{when:%b} {when.day}, {when.year}"


def trim_date(title: str, *, today: date) -> str:
    """A card's whole text used as its title, cut back to the name.

    "The AI Conference Sep . 30 - Oct 01 , 2026 San Francisco, CA" -> "The AI
    Conference"; "October 28-29, 2026 GitHub Universe" -> "GitHub Universe".
    """
    found = find_date(title, today=today)
    if found is None:
        return title
    start = title.find(found[1])
    head = title[:start].strip(" \u00b7|-\u2013\u2014,:")
    tail = title[start + len(found[1]) :].strip(" \u00b7|-\u2013\u2014,:")
    if len(head) >= 4:
        return head
    return tail or title


def with_date(title: str, when: date | None, raw: str | None = None) -> str:
    """``title · Nov 4, 2026``, unless the title already says when."""
    if when is None:
        return title
    if raw and raw.lower() in title.lower():
        return title
    if find_date(title, today=when) is not None:
        return title
    return f"{title} · {format_day(when)}"


def event_id(value: str) -> str:
    return value if value.startswith("event:") else f"event:{value}"


def slug_title(url: str) -> str:
    """A readable title from the last path segment: ``/phd-summit/`` -> ``PhD Summit``."""
    slug = url.rstrip("/").rsplit("/", 1)[-1].split("?", 1)[0]
    # CMS-generated suffixes: "emnlp-2026-2026-10-24t16-00-00-000z" -> "emnlp-2026"
    slug = re.sub(r"-\d{4}-\d\d-\d\dt[\d-]+z?$", "", slug, flags=re.I)
    words = [word for word in re.split(r"[-_]+", slug) if word]
    small = {"and", "or", "of", "the", "for", "in", "at", "to", "a", "an", "on"}
    known = {
        "phd": "PhD", "ai": "AI", "ml": "ML", "gqs": "GQS", "nyc": "NYC", "us": "US",
        "emea": "EMEA", "apac": "APAC", "amer": "AMER", "uk": "UK", "neurips": "NeurIPS",
    }  # fmt: skip
    out = []
    for index, word in enumerate(words):
        lower = word.lower()
        if lower in known:
            out.append(known[lower])
        elif index and lower in small:
            out.append(lower)
        else:
            out.append(lower.capitalize())
    return " ".join(out)


__all__: Sequence[str] = (
    "PAST_GRACE",
    "event_id",
    "find_date",
    "format_day",
    "is_past",
    "slug_title",
    "text_of",
    "trim_date",
    "with_date",
)
