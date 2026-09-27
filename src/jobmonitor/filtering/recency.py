"""Posting-age window: only postings from the last day are ever seen.

Applied once, at the point every adapter's output flows through
(:func:`jobmonitor.orchestration.pipeline.process_company`), so all nine
providers obey an identical rule and none of them has to know about it. The
adapters stay pure parsers, which is also what keeps their fixture tests
independent of the wall clock.

Three kinds of date need three different treatments. Each choice below errs
toward *keeping* a posting, because a missed internship costs far more than one
extra line in a digest.

**A real timestamp** (Greenhouse ``first_published``, Lever ``createdAt``, Ashby
``publishedAt``, ...): kept if it falls inside the window. A date in the future -
clock skew, or a posting scheduled ahead - is kept.

**A day-granular date** (Workday says only "Posted Yesterday"): the adapter
records midnight of that day. Compared literally, a job posted at 8pm yesterday
would look 39 hours old at 3pm today and be dropped. So a timestamp at exactly
00:00:00.000000 UTC is read as *any time during that day*, and kept if any part
of the day overlaps the window. A genuine posting at that exact microsecond is
treated the same way, which can only ever keep something, never lose it.

**No date at all** (Rippling's job list has none): the posting cannot be shown
to be old, so by default it is kept, and the ordinary seen-before check stops it
alerting twice. ``keep_undated=False`` drops them instead.

A note on fallbacks: several adapters fall back to an *updated* timestamp when
the posting date is missing. That is safe here because an update can only happen
after publication - if the updated time is already outside the window, the
posting certainly is too. The fallback can admit an old posting that was edited
recently, but it can never hide a new one.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from jobmonitor.models.job import Job

DAY = timedelta(days=1)


def is_day_precision(value: datetime) -> bool:
    """True when a timestamp carries only a date (exactly midnight UTC)."""
    moment = value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    return (moment.hour, moment.minute, moment.second, moment.microsecond) == (0, 0, 0, 0)


def latest_possible_posting_time(value: datetime) -> datetime:
    """The latest moment the posting could have gone up, given what the source told us."""
    moment = value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    return moment + DAY if is_day_precision(moment) else moment


def is_recent(
    job: Job,
    *,
    now: datetime,
    max_age: timedelta,
    keep_undated: bool = True,
) -> bool:
    """Whether ``job`` could have been posted within ``max_age`` of ``now``."""
    if job.date_posted is None:
        return keep_undated
    reference = now.astimezone(UTC) if now.tzinfo else now.replace(tzinfo=UTC)
    return latest_possible_posting_time(job.date_posted) >= reference - max_age


@dataclass(slots=True)
class RecencyResult:
    """The split, kept so the health record can say how much was filtered."""

    recent: list[Job] = field(default_factory=list)
    too_old: list[Job] = field(default_factory=list)
    undated: int = 0

    @property
    def dropped(self) -> int:
        return len(self.too_old)


def split_by_recency(
    jobs: Iterable[Job],
    *,
    now: datetime,
    max_age: timedelta | None,
    keep_undated: bool = True,
) -> RecencyResult:
    """Partition ``jobs``. ``max_age=None`` disables the window and keeps everything."""
    result = RecencyResult()
    for job in jobs:
        if job.date_posted is None:
            result.undated += 1
        if max_age is None or is_recent(job, now=now, max_age=max_age, keep_undated=keep_undated):
            result.recent.append(job)
        else:
            result.too_old.append(job)
    return result


__all__: Sequence[str] = (
    "DAY",
    "RecencyResult",
    "is_day_precision",
    "is_recent",
    "latest_possible_posting_time",
    "split_by_recency",
)
