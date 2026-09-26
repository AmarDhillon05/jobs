"""Level 1 - normalization of a scraped posting into the canonical Job."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from jobmonitor.errors import InvalidJobError
from jobmonitor.models.job import MAX_DESCRIPTION_CHARS, Job, clean_text, strip_html


def make_job(**kwargs: object) -> Job:
    payload: dict[str, object] = {
        "company": "TestCo",
        "title": "Software Engineer Intern",
        "url": "https://job-boards.greenhouse.io/testco/jobs/1",
        "source": "greenhouse",
    }
    payload.update(kwargs)
    return Job(**payload)  # type: ignore[arg-type]


class TestCleanText:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("  Software   Engineer\n Intern ", "Software Engineer Intern"),
            ("R&amp;D Intern", "R&D Intern"),
            ("Dev Intern", "Dev Intern"),
            ("Intern &ndash; Backend", "Intern - Backend"),
            ("", None),
            ("   ", None),
            (None, None),
        ],
    )
    def test_collapses_whitespace_and_entities(self, raw: str | None, expected: str | None) -> None:
        assert clean_text(raw) == expected


class TestStripHtml:
    def test_removes_tags_and_keeps_word_boundaries(self) -> None:
        html = "<p>Build <b>systems</b>.</p><p>Ship them.</p>"
        assert strip_html(html) == "Build systems . Ship them."

    def test_list_items_do_not_glue_together(self) -> None:
        assert strip_html("<ul><li>Python</li><li>Go</li></ul>") == "Python Go"

    def test_none_passes_through(self) -> None:
        assert strip_html(None) is None


class TestRequiredFields:
    def test_minimal_job_is_valid(self) -> None:
        job = make_job()
        assert job.company == "TestCo"
        assert job.source == "greenhouse"

    @pytest.mark.parametrize("field", ["company", "title", "url", "source"])
    def test_missing_required_field_raises(self, field: str) -> None:
        with pytest.raises(InvalidJobError, match=f"'{field}'|missing required"):
            make_job(**{field: ""})

    @pytest.mark.parametrize("field", ["company", "title", "source"])
    def test_whitespace_only_required_field_raises(self, field: str) -> None:
        with pytest.raises(InvalidJobError):
            make_job(**{field: "   \n "})

    def test_titles_and_companies_are_whitespace_normalized(self) -> None:
        job = make_job(company="  Test Co \n", title="Software   Engineer\tIntern")
        assert job.company == "Test Co"
        assert job.title == "Software Engineer Intern"


class TestUrlValidation:
    @pytest.mark.parametrize(
        "url",
        [
            "https://job-boards.greenhouse.io/testco/jobs/1",
            "http://example.com/x",
            "https://example.com/jobs?id=1&loc=sf",
        ],
    )
    def test_valid_urls_accepted(self, url: str) -> None:
        assert make_job(url=url).url == url

    @pytest.mark.parametrize(
        "url",
        [
            "/relative/path",
            "ftp://example.com/job",
            "mailto:jobs@example.com",
            "javascript:alert(1)",
            "https://",
            "https://localhost/job",  # no dot in host
            "https://example.com/a b",
        ],
    )
    def test_invalid_urls_rejected(self, url: str) -> None:
        with pytest.raises(InvalidJobError):
            make_job(url=url)


class TestDatePosted:
    def test_iso_string_with_z(self) -> None:
        job = make_job(date_posted="2026-09-26T12:30:00Z")
        assert job.date_posted == datetime(2026, 9, 26, 12, 30, tzinfo=UTC)

    def test_iso_string_with_offset(self) -> None:
        job = make_job(date_posted="2026-09-26T12:30:00+00:00")
        assert job.date_posted is not None
        assert job.date_posted.tzinfo is not None

    def test_date_only_string(self) -> None:
        assert make_job(date_posted="2026-09-26").date_posted == datetime(2026, 9, 26, tzinfo=UTC)

    def test_human_readable_string(self) -> None:
        assert make_job(date_posted="September 26, 2026").date_posted == datetime(
            2026, 9, 26, tzinfo=UTC
        )

    def test_epoch_seconds(self) -> None:
        assert make_job(date_posted=1769728646).date_posted == datetime.fromtimestamp(
            1769728646, tz=UTC
        )

    def test_epoch_milliseconds(self) -> None:
        # Ashby and Workday both emit millisecond timestamps in places.
        assert make_job(date_posted=1769728646000).date_posted == datetime.fromtimestamp(
            1769728646, tz=UTC
        )

    def test_naive_datetime_is_assumed_utc(self) -> None:
        job = make_job(date_posted=datetime(2026, 9, 26, 12, 0))
        assert job.date_posted == datetime(2026, 9, 26, 12, 0, tzinfo=UTC)

    @pytest.mark.parametrize("value", ["", "   ", "not a date", None, {}, 1e30])
    def test_unreadable_dates_become_none_rather_than_dropping_the_job(self, value: object) -> None:
        # PRD §9: first_seen is authoritative, so a bad posting date is harmless.
        assert make_job(date_posted=value).date_posted is None


class TestDescription:
    def test_html_is_stripped(self) -> None:
        assert make_job(description="<p>Write <i>code</i></p>").description == "Write code"

    def test_long_descriptions_are_truncated(self) -> None:
        job = make_job(description="x" * (MAX_DESCRIPTION_CHARS + 500))
        assert job.description is not None
        assert len(job.description) <= MAX_DESCRIPTION_CHARS + 3
        assert job.description.endswith("...")

    def test_preview_truncates_on_word_boundary_length(self) -> None:
        job = make_job(description="a" * 400)
        assert len(job.description_preview(100)) == 103

    def test_preview_of_short_description_is_unchanged(self) -> None:
        job = make_job(description="Short one")
        assert job.description_preview() == "Short one"

    def test_preview_of_missing_description_is_empty(self) -> None:
        assert make_job().description_preview() == ""


class TestMisc:
    def test_external_id_flag(self) -> None:
        assert make_job(external_id="4020160008").has_stable_external_id
        assert not make_job().has_stable_external_id
        assert not make_job(external_id="  ").has_stable_external_id

    def test_job_is_immutable(self) -> None:
        with pytest.raises((AttributeError, TypeError)):
            make_job().title = "other"  # type: ignore[misc]

    def test_with_fields_returns_a_copy(self) -> None:
        job = make_job()
        other = job.with_fields(title="Backend Engineer Intern")
        assert job.title == "Software Engineer Intern"
        assert other.title == "Backend Engineer Intern"

    def test_round_trips_through_dict(self) -> None:
        job = make_job(
            location="San Francisco, CA",
            external_id="42",
            date_posted="2026-09-26T00:00:00Z",
            description="Build things",
            employment_type="Intern",
        )
        assert Job.from_dict(job.to_dict()) == job

    def test_to_dict_serialises_date_as_iso_or_none(self) -> None:
        assert make_job().to_dict()["date_posted"] is None
        assert make_job(date_posted="2026-09-26").to_dict()["date_posted"].startswith("2026-09-26")

    def test_equality_is_by_value(self) -> None:
        assert make_job() == make_job()
        assert make_job() != make_job(title="Other Intern")
