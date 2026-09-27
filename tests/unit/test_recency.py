"""The one-day posting window (jobmonitor.filtering.recency).

The rule the user asked for: postings from the last day are seen, anything older
is not. The interesting cases are the ones where a literal comparison gives the
wrong answer - day-only dates, the exact boundary, timezones - so each gets a test
named for the mistake it prevents.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from jobmonitor.config import ConfigError, FilterSettings, Settings, for_tests
from jobmonitor.filtering.recency import (
    is_day_precision,
    is_recent,
    latest_possible_posting_time,
    split_by_recency,
)
from jobmonitor.models.job import Job
from jobmonitor.scrapers.workday import parse_posted_on

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 27, 15, 0, tzinfo=UTC)  # 3pm on a Sunday
DAY = timedelta(days=1)


def job(date_posted: datetime | str | None) -> Job:
    return Job(
        company="TestCo",
        title="Software Engineer Intern",
        url="https://example.test/jobs/1",
        source="greenhouse",
        external_id="1",
        date_posted=date_posted,  # type: ignore[arg-type]
    )


def recent(date_posted: datetime | str | None, **kwargs: object) -> bool:
    return is_recent(job(date_posted), now=NOW, max_age=DAY, **kwargs)  # type: ignore[arg-type]


class TestRealTimestamps:
    @pytest.mark.parametrize(
        ("age", "expected"),
        [
            (timedelta(minutes=5), True),
            (timedelta(hours=23, minutes=59), True),
            (DAY, True),  # the boundary is inclusive
            (DAY + timedelta(seconds=1), False),
            (timedelta(days=3), False),
            (timedelta(days=45), False),
        ],
    )
    def test_the_window_is_one_day(self, age: timedelta, expected: bool) -> None:
        assert recent(NOW - age) is expected

    def test_a_future_date_is_kept(self) -> None:
        # Clock skew, or a posting scheduled ahead: it is not old, so keep it.
        assert recent(NOW + timedelta(hours=6)) is True

    def test_the_real_greenhouse_example_is_judged_on_first_published(self) -> None:
        # Observed live on Stripe's board: first published Sep 3, edited Sep 25.
        # The adapter reads first_published, so an edit does not make it "new".
        assert recent("2026-09-03T13:32:53-04:00") is False

    def test_offsets_are_compared_as_instants(self) -> None:
        # 11pm in New York on the 26th is 3am UTC on the 27th: twelve hours ago.
        assert recent("2026-09-26T23:00:00-04:00") is True
        # 1am in New York on the 26th is 5am UTC on the 26th: 34 hours ago.
        assert recent("2026-09-26T01:00:00-04:00") is False

    def test_a_naive_timestamp_is_read_as_utc(self) -> None:
        assert recent(datetime(2026, 9, 27, 9, 0)) is True
        assert recent(datetime(2026, 9, 20, 9, 0)) is False

    def test_a_naive_now_is_read_as_utc(self) -> None:
        naive_now = datetime(2026, 9, 27, 15, 0)
        assert is_recent(job(NOW - timedelta(hours=2)), now=naive_now, max_age=DAY) is True


class TestDayOnlyDates:
    """Workday says "Posted Yesterday", not a time. Read literally, that loses jobs."""

    def test_midnight_utc_is_recognised_as_day_precision(self) -> None:
        assert is_day_precision(datetime(2026, 9, 26, tzinfo=UTC)) is True
        assert is_day_precision(datetime(2026, 9, 26, 0, 0, 1, tzinfo=UTC)) is False
        assert is_day_precision(datetime(2026, 9, 26)) is True  # naive, read as UTC

    def test_midnight_in_another_zone_is_not_day_precision(self) -> None:
        # 00:00 in New York is 04:00 UTC - a real time of day, not a bare date.
        ny = timezone(timedelta(hours=-4))
        assert is_day_precision(datetime(2026, 9, 26, 0, 0, tzinfo=ny)) is False

    def test_a_day_only_date_can_be_as_late_as_the_end_of_that_day(self) -> None:
        day = datetime(2026, 9, 26, tzinfo=UTC)
        assert latest_possible_posting_time(day) == datetime(2026, 9, 27, tzinfo=UTC)

    def test_a_real_timestamp_is_taken_at_face_value(self) -> None:
        moment = datetime(2026, 9, 26, 14, 30, tzinfo=UTC)
        assert latest_possible_posting_time(moment) == moment

    def test_posted_yesterday_is_still_seen_at_3pm_today(self) -> None:
        # The bug this module exists to prevent: yesterday 00:00 is 39h before 3pm
        # today, but a job posted at 8pm yesterday is only 19h old.
        assert recent(parse_posted_on("Posted Yesterday", now=NOW)) is True

    @pytest.mark.parametrize("hour", [0, 6, 12, 18, 23])
    def test_posted_today_is_seen_at_any_hour(self, hour: int) -> None:
        now = NOW.replace(hour=hour)
        posted = parse_posted_on("Posted Today", now=now)
        assert is_recent(job(posted), now=now, max_age=DAY) is True

    @pytest.mark.parametrize(
        "text", ["Posted 2 Days Ago", "Posted 5 Days Ago", "Posted 30+ Days Ago"]
    )
    def test_older_workday_postings_are_dropped(self, text: str) -> None:
        assert recent(parse_posted_on(text, now=NOW)) is False

    def test_a_real_timestamp_that_lands_on_utc_midnight_is_only_ever_over_included(
        self,
    ) -> None:
        # 8pm in New York is exactly midnight UTC, so it is read as "some time that
        # day". The cost of that ambiguity is keeping a posting, never losing one.
        ny = timezone(timedelta(hours=-4))
        posted = datetime(2026, 9, 25, 20, 0, tzinfo=ny)  # == 2026-09-26T00:00Z
        assert recent(posted) is True


class TestUndatedPostings:
    def test_are_kept_by_default(self) -> None:
        # Rippling's job list carries no date at all; it cannot be shown to be old.
        assert recent(None) is True

    def test_can_be_dropped_instead(self) -> None:
        assert recent(None, keep_undated=False) is False


class TestSplit:
    def test_partitions_and_counts(self) -> None:
        jobs = [
            job(NOW - timedelta(hours=1)),
            job(NOW - timedelta(days=4)),
            job(None),
            job(parse_posted_on("Posted Yesterday", now=NOW)),
        ]
        result = split_by_recency(jobs, now=NOW, max_age=DAY)
        assert len(result.recent) == 3
        assert result.dropped == 1
        assert result.undated == 1

    def test_no_window_keeps_everything(self) -> None:
        jobs = [job(NOW - timedelta(days=400)), job(None)]
        result = split_by_recency(jobs, now=NOW, max_age=None)
        assert len(result.recent) == 2
        assert result.dropped == 0

    def test_an_empty_board_is_fine(self) -> None:
        result = split_by_recency([], now=NOW, max_age=DAY)
        assert result.recent == [] and result.dropped == 0


class TestConfiguration:
    def test_production_default_is_one_day(self) -> None:
        settings = Settings.from_env({})
        assert settings.filters.max_posting_age == DAY
        assert settings.filters.keep_undated is True

    def test_the_window_is_configurable(self) -> None:
        settings = Settings.from_env({"MAX_POSTING_AGE_HOURS": "48"})
        assert settings.filters.max_posting_age == timedelta(hours=48)

    def test_fractional_hours_are_allowed(self) -> None:
        settings = Settings.from_env({"MAX_POSTING_AGE_HOURS": "1.5"})
        assert settings.filters.max_posting_age == timedelta(minutes=90)

    @pytest.mark.parametrize("value", ["0", "off", "OFF", "none", "disabled"])
    def test_the_window_can_be_switched_off(self, value: str) -> None:
        settings = Settings.from_env({"MAX_POSTING_AGE_HOURS": value})
        assert settings.filters.max_posting_age is None

    @pytest.mark.parametrize("value", ["-1", "yesterday", "24h"])
    def test_nonsense_is_rejected_loudly(self, value: str) -> None:
        with pytest.raises(ConfigError, match="MAX_POSTING_AGE_HOURS"):
            Settings.from_env({"MAX_POSTING_AGE_HOURS": value})

    def test_undated_postings_can_be_dropped_by_config(self) -> None:
        settings = Settings.from_env({"KEEP_UNDATED_POSTINGS": "false"})
        assert settings.filters.keep_undated is False

    def test_test_settings_switch_the_window_off(self) -> None:
        # Deliberate, and documented in for_tests(): fixture dates are fixed.
        assert for_tests().filters.max_posting_age is None

    def test_an_explicit_filter_setting_turns_it_back_on(self) -> None:
        assert for_tests(filters=FilterSettings()).filters.max_posting_age == DAY
