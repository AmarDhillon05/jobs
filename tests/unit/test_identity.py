"""Level 1 - canonical URL logic, stable job IDs, content hashes, dedup."""

from __future__ import annotations

import pytest

from jobmonitor.identity import (
    canonical_url,
    company_slug,
    content_hash,
    deduplicate,
    job_id,
    normalize_location,
    normalize_title,
)
from jobmonitor.models.job import Job


def make_job(**kwargs: object) -> Job:
    payload: dict[str, object] = {
        "company": "TestCo",
        "title": "Software Engineer Intern",
        "url": "https://job-boards.greenhouse.io/testco/jobs/1",
        "source": "greenhouse",
    }
    payload.update(kwargs)
    return Job(**payload)  # type: ignore[arg-type]


class TestCanonicalUrl:
    def test_strips_tracking_parameters(self) -> None:
        assert (
            canonical_url(
                "https://job-boards.greenhouse.io/testco/jobs/1?utm_source=github&gh_src=abc"
            )
            == "https://job-boards.greenhouse.io/testco/jobs/1"
        )

    def test_keeps_meaningful_parameters(self) -> None:
        # Workday and others carry the posting id in the query string.
        assert canonical_url("https://x.example.com/job?jobId=42&utm_medium=email") == (
            "https://x.example.com/job?jobId=42"
        )

    def test_sorts_remaining_parameters(self) -> None:
        a = canonical_url("https://x.example.com/j?b=2&a=1")
        b = canonical_url("https://x.example.com/j?a=1&b=2")
        assert a == b == "https://x.example.com/j?a=1&b=2"

    def test_lowercases_scheme_and_host_but_not_path(self) -> None:
        assert canonical_url("HTTPS://Jobs.Example.COM/Job/Intern") == (
            "https://jobs.example.com/Job/Intern"
        )

    def test_removes_default_ports_but_keeps_others(self) -> None:
        assert canonical_url("https://x.example.com:443/j") == "https://x.example.com/j"
        assert canonical_url("http://x.example.com:80/j") == "http://x.example.com/j"
        assert canonical_url("https://x.example.com:8443/j") == "https://x.example.com:8443/j"

    def test_drops_fragment(self) -> None:
        assert canonical_url("https://x.example.com/j#apply") == "https://x.example.com/j"

    def test_strips_trailing_slash_but_keeps_root(self) -> None:
        assert canonical_url("https://x.example.com/j/") == "https://x.example.com/j"
        assert canonical_url("https://x.example.com/") == "https://x.example.com/"
        assert canonical_url("https://x.example.com") == "https://x.example.com/"

    def test_is_idempotent(self) -> None:
        once = canonical_url("https://X.example.com:443/j/?utm_source=a&b=1#frag")
        assert canonical_url(once) == once

    def test_empty_url(self) -> None:
        assert canonical_url("") == ""

    def test_two_tracking_variants_of_one_posting_agree(self) -> None:
        # This is the case that would otherwise notify the user twice.
        simplify = "https://jobs.ashbyhq.com/notion/abc?utm_source=Simplify&ref=Simplify"
        ouckah = "https://jobs.ashbyhq.com/notion/abc?utm_source=github-vansh-ouckah"
        assert canonical_url(simplify) == canonical_url(ouckah)


class TestNormalizeTitle:
    def test_drops_season_and_location_decorations(self) -> None:
        assert normalize_title("Software Engineer Intern (Summer 2027) - Remote") == (
            normalize_title("Software Engineer Intern")
        )

    def test_separator_and_case_differences_collapse(self) -> None:
        assert normalize_title("Software Engineer, Intern") == normalize_title(
            "software engineer / intern"
        )

    def test_word_order_does_not_matter(self) -> None:
        assert normalize_title("Intern, Software Engineer") == normalize_title(
            "Software Engineer Intern"
        )

    def test_different_roles_stay_different(self) -> None:
        assert normalize_title("Software Engineer Intern") != normalize_title(
            "Hardware Engineer Intern"
        )

    def test_empty_title(self) -> None:
        assert normalize_title("") == ""


class TestNormalizeLocation:
    @pytest.mark.parametrize(
        ("a", "b"),
        [
            ("SF", "San Francisco"),
            ("NYC", "New York"),
            ("San Francisco, CA", "San Francisco CA"),
            ("Remote - US", "Remote US"),
        ],
    )
    def test_equivalent_spellings_agree(self, a: str, b: str) -> None:
        assert normalize_location(a) == normalize_location(b)

    def test_distinct_locations_differ(self) -> None:
        assert normalize_location("Seattle") != normalize_location("Austin")

    def test_none_and_empty(self) -> None:
        assert normalize_location(None) == ""
        assert normalize_location("") == ""


class TestCompanySlug:
    @pytest.mark.parametrize(
        ("name", "slug"),
        [
            ("TestCo", "testco"),
            ("Jane Street", "jane-street"),
            ("D. E. Shaw", "d-e-shaw"),
            ("1Password", "1password"),
        ],
    )
    def test_slugs(self, name: str, slug: str) -> None:
        assert company_slug(name) == slug


class TestJobId:
    def test_prefers_provider_external_id(self) -> None:
        assert job_id(make_job(external_id="4020160008")) == "testco:4020160008"

    def test_external_id_survives_title_and_url_changes(self) -> None:
        # The whole point of preferring the provider's id.
        a = make_job(external_id="42", title="SWE Intern")
        b = make_job(external_id="42", title="Software Engineer Intern (Summer 2027)")
        assert job_id(a) == job_id(b)

    def test_falls_back_to_a_hash_without_an_external_id(self) -> None:
        identity = job_id(make_job())
        assert identity.startswith("testco:h:")
        assert len(identity) == len("testco:h:") + 32

    def test_fallback_is_stable_across_tracking_parameters(self) -> None:
        a = make_job(url="https://job-boards.greenhouse.io/testco/jobs/1?utm_source=x")
        b = make_job(url="https://job-boards.greenhouse.io/testco/jobs/1")
        assert job_id(a) == job_id(b)

    def test_fallback_is_stable_across_title_decoration(self) -> None:
        a = make_job(title="Software Engineer Intern (Summer 2027)")
        b = make_job(title="Software Engineer Intern")
        assert job_id(a) == job_id(b)

    def test_fallback_distinguishes_different_locations(self) -> None:
        assert job_id(make_job(location="Seattle")) != job_id(make_job(location="Austin"))

    def test_fallback_distinguishes_different_companies(self) -> None:
        assert job_id(make_job(company="A Co")) != job_id(make_job(company="B Co"))

    def test_fallback_distinguishes_different_urls(self) -> None:
        a = make_job(url="https://job-boards.greenhouse.io/testco/jobs/1")
        b = make_job(url="https://job-boards.greenhouse.io/testco/jobs/2")
        assert job_id(a) != job_id(b)

    def test_id_is_deterministic_across_calls(self) -> None:
        assert job_id(make_job()) == job_id(make_job())


class TestContentHash:
    def test_identical_jobs_hash_identically(self) -> None:
        assert content_hash(make_job()) == content_hash(make_job())

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("title", "Backend Engineer Intern"),
            ("location", "Austin, TX"),
            ("description", "Different work"),
            ("employment_type", "Part-time"),
            ("date_posted", "2026-01-01"),
            ("url", "https://job-boards.greenhouse.io/testco/jobs/2"),
        ],
    )
    def test_user_visible_changes_change_the_hash(self, field: str, value: str) -> None:
        assert content_hash(make_job()) != content_hash(make_job(**{field: value}))

    def test_tracking_parameters_do_not_change_the_hash(self) -> None:
        a = make_job()
        b = make_job(url="https://job-boards.greenhouse.io/testco/jobs/1?utm_source=x")
        assert content_hash(a) == content_hash(b)

    def test_external_id_is_not_part_of_the_content_hash(self) -> None:
        # external_id is identity, not content: gaining one must not look like an edit.
        assert content_hash(make_job()) == content_hash(make_job(external_id="42"))


class TestDeduplicate:
    def test_removes_duplicates_within_one_response(self) -> None:
        jobs = [make_job(external_id="1"), make_job(external_id="1"), make_job(external_id="2")]
        assert [j.external_id for j in deduplicate(jobs)] == ["1", "2"]

    def test_first_occurrence_wins(self) -> None:
        first = make_job(external_id="1", location="Seattle")
        second = make_job(external_id="1", location="Austin")
        assert deduplicate([first, second]) == [first]

    def test_collapses_tracking_url_duplicates_without_external_ids(self) -> None:
        a = make_job(url="https://job-boards.greenhouse.io/testco/jobs/1")
        b = make_job(url="https://job-boards.greenhouse.io/testco/jobs/1?utm_source=x")
        assert len(deduplicate([a, b])) == 1

    def test_keeps_genuinely_different_jobs(self) -> None:
        jobs = [make_job(external_id="1"), make_job(external_id="2", title="ML Intern")]
        assert len(deduplicate(jobs)) == 2

    def test_empty_input(self) -> None:
        assert deduplicate([]) == []
