"""Level 1 - company discovery: alias normalization and provider detection."""

from __future__ import annotations

import pytest

from jobmonitor.discovery import (
    aggregate_listings,
    careers_url_for,
    detect_provider,
    normalize_company_name,
)


class TestNormalizeCompanyName:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Stripe", "stripe"),
            ("Stripe, Inc.", "stripe"),
            ("STRIPE INC", "stripe"),
            ("  stripe  ", "stripe"),
            ("D. E. Shaw", "deshaw"),
            ("DE Shaw", "deshaw"),
            # "Trading" is meaningful (cf. Belvedere Trading) so it is kept;
            # only legal/generic suffixes are stripped.
            ("Hudson River Trading LLC", "hudsonrivertrading"),
            ("Jane Street Capital", "janestreetcapital"),
            ("Teledyne Technologies Incorporated", "teledyne"),
            ("The Nuclear Company", "nuclear"),
            ("Two Sigma", "twosigma"),
            ("Fly.io", "flyio"),
            ("1Password", "1password"),
        ],
    )
    def test_canonicalisation(self, raw: str, expected: str) -> None:
        assert normalize_company_name(raw) == expected

    def test_aliases_collapse_to_the_same_key(self) -> None:
        variants = ["Cockroach Labs", "Cockroach Labs, Inc.", "cockroach labs"]
        assert len({normalize_company_name(v) for v in variants}) == 1

    def test_legend_emoji_and_parentheticals_are_stripped(self) -> None:
        assert normalize_company_name("Acme \U0001f6c2 (US only)") == "acme"

    def test_trailing_program_qualifier_is_dropped(self) -> None:
        assert normalize_company_name("Acme - University Program") == "acme"

    def test_distinct_companies_do_not_collide(self) -> None:
        # "Labs"/"Systems" stripping must not merge genuinely different firms.
        assert normalize_company_name("Scale AI") != normalize_company_name("Scale Computing")
        assert normalize_company_name("Citadel") != normalize_company_name("Citadel Securities")

    def test_name_of_only_suffixes_returns_empty(self) -> None:
        assert normalize_company_name("Inc.") == ""


class TestDetectProvider:
    @pytest.mark.parametrize(
        ("url", "provider", "config"),
        [
            (
                "https://job-boards.greenhouse.io/anthropic/jobs/4020160008",
                "greenhouse",
                {"board_token": "anthropic"},
            ),
            (
                "https://boards.greenhouse.io/figma/jobs/12345",
                "greenhouse",
                {"board_token": "figma"},
            ),
            (
                "https://acmecorp.greenhouse.io/acmecorp/jobs/1",
                "greenhouse",
                {"board_token": "acmecorp"},
            ),
            ("https://jobs.lever.co/palantir/abc-def", "lever", {"site": "palantir"}),
            (
                "https://jobs.ashbyhq.com/openai/1ba0a1ef-0e29",
                "ashby",
                {"job_board_name": "openai"},
            ),
            (
                "https://jobs.smartrecruiters.com/Canva/744000-engineer",
                "smartrecruiters",
                {"company_id": "Canva"},
            ),
            (
                "https://apply.workable.com/acme/j/ABC123/",
                "workable",
                {"subdomain": "acme"},
            ),
            (
                "https://ats.rippling.com/en-GB/rippling/jobs/3fd9615a",
                "rippling",
                {"board_slug": "rippling"},
            ),
        ],
    )
    def test_supported_providers(self, url: str, provider: str, config: dict[str, str]) -> None:
        guess = detect_provider(url)
        assert guess.provider == provider
        assert guess.config == config
        assert guess.supported is True
        assert guess.is_configured is True

    def test_workday_tenant_datacenter_and_site(self) -> None:
        guess = detect_provider(
            "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite"
            "/job/US-CA/Software-Intern_JR123"
        )
        assert guess.provider == "workday"
        assert guess.config == {
            "tenant": "nvidia",
            "host": "nvidia.wd5.myworkdayjobs.com",
            "site": "NVIDIAExternalCareerSite",
        }
        assert guess.is_configured

    def test_workday_without_locale_prefix(self) -> None:
        guess = detect_provider(
            "https://intel.wd1.myworkdayjobs.com/External/job/Folsom/Intern_JR1"
        )
        assert guess.config["site"] == "External"
        assert guess.config["tenant"] == "intel"

    def test_workday_multi_segment_locale(self) -> None:
        guess = detect_provider(
            "https://globalhr.wd5.myworkdayjobs.com/fr-CA/Private_Posting/job/US-MA/Dev_01"
        )
        assert guess.config["site"] == "Private_Posting"

    def test_greenhouse_embed_url_yields_unconfigured_guess(self) -> None:
        guess = detect_provider("https://boards.greenhouse.io/embed/job_app?token=999")
        assert guess.provider == "greenhouse"
        assert guess.config == {}
        assert guess.is_configured is False
        assert "embed" in guess.reason

    def test_myworkdaysite_is_recognised_but_unsupported(self) -> None:
        guess = detect_provider("https://wd1.myworkdaysite.com/recruiting/snap/snap/job/1")
        assert guess.supported is False
        assert guess.is_configured is False
        assert guess.reason

    @pytest.mark.parametrize(
        ("url", "provider"),
        [
            ("https://careers-gdms.icims.com/jobs/12345/intern/job", "icims"),
            (
                "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX/job/1",
                "oracle",
            ),
            ("https://jobs.jobvite.com/acme/job/oABC", "jobvite"),
            ("https://qualcomm.eightfold.ai/careers/job?pid=1", "eightfold"),
            ("https://twosigma.avature.net/careers/JobDetail/1", "avature"),
            ("https://githubinc.jibeapply.com/jobs/1", "jibeapply"),
        ],
    )
    def test_recognised_but_unsupported_providers(self, url: str, provider: str) -> None:
        guess = detect_provider(url)
        assert guess.provider == provider
        assert guess.supported is False
        assert guess.reason

    @pytest.mark.parametrize(
        "url",
        ["https://www.tesla.com/careers/search/job/1", "https://amazon.jobs/en/jobs/1"],
    )
    def test_unknown_hosts_become_custom(self, url: str) -> None:
        guess = detect_provider(url)
        assert guess.provider == "custom"
        assert guess.supported is False

    @pytest.mark.parametrize("url", ["", "   ", "not-a-url", "mailto:a@b.com"])
    def test_degenerate_urls_do_not_raise(self, url: str) -> None:
        guess = detect_provider(url)
        assert guess.is_configured is False

    def test_port_suffix_is_tolerated(self) -> None:
        guess = detect_provider("https://job-boards.greenhouse.io:443/acme/jobs/1")
        assert guess.config == {"board_token": "acme"}


class TestCareersUrlFor:
    @pytest.mark.parametrize(
        ("provider", "config", "expected"),
        [
            (
                "greenhouse",
                {"board_token": "anthropic"},
                "https://job-boards.greenhouse.io/anthropic",
            ),
            ("lever", {"site": "palantir"}, "https://jobs.lever.co/palantir"),
            ("ashby", {"job_board_name": "openai"}, "https://jobs.ashbyhq.com/openai"),
            ("smartrecruiters", {"company_id": "Canva"}, "https://jobs.smartrecruiters.com/Canva"),
            ("workable", {"subdomain": "acme"}, "https://apply.workable.com/acme/"),
            ("rippling", {"board_slug": "r"}, "https://ats.rippling.com/r/jobs"),
            (
                "workday",
                {"host": "nvidia.wd5.myworkdayjobs.com", "site": "Ext"},
                "https://nvidia.wd5.myworkdayjobs.com/en-US/Ext",
            ),
        ],
    )
    def test_builds_canonical_urls(
        self, provider: str, config: dict[str, str], expected: str
    ) -> None:
        assert careers_url_for(provider, config) == expected

    def test_returns_none_for_incomplete_config(self) -> None:
        assert careers_url_for("greenhouse", {}) is None
        assert careers_url_for("custom", {"x": 1}) is None


class TestAggregateListings:
    def _rows(self) -> list[dict[str, object]]:
        return [
            {
                "company_name": "Stripe",
                "url": "https://job-boards.greenhouse.io/stripe/jobs/1",
                "source": "Simplify",
            },
            {
                "company_name": "Stripe, Inc.",
                "url": "https://job-boards.greenhouse.io/stripe/jobs/2",
                "source": "vanshb03",
            },
            {
                "company_name": "Stripe",
                "url": "https://boards.greenhouse.io/embed/job_app?token=9",
                "source": "Simplify",
            },
            {
                "company_name": "Notion",
                "url": "https://jobs.ashbyhq.com/notion/abc",
                "source": "Simplify",
            },
            # Partial rows: community data routinely contains these.
            {"company_name": "", "url": "https://job-boards.greenhouse.io/x/jobs/1"},
            {"company_name": "Ghost", "url": ""},
            {"company_name": "Inc.", "url": "https://jobs.lever.co/a/b"},
        ]

    def test_aliases_are_merged_into_one_company(self) -> None:
        companies = aggregate_listings(self._rows())
        by_key = {c.normalized: c for c in companies}
        assert set(by_key) == {"stripe", "notion"}
        stripe = by_key["stripe"]
        assert stripe.listing_count == 3
        assert stripe.observed_names == ("Stripe", "Stripe, Inc.")
        assert stripe.canonical_name == "Stripe"  # most frequently observed spelling

    def test_sources_are_recorded_per_company(self) -> None:
        by_key = {c.normalized: c for c in aggregate_listings(self._rows())}
        assert by_key["stripe"].sources == ("Simplify", "vanshb03")
        assert by_key["notion"].sources == ("Simplify",)

    def test_best_guess_prefers_a_configured_provider(self) -> None:
        by_key = {c.normalized: c for c in aggregate_listings(self._rows())}
        guess = by_key["stripe"].best_guess
        assert guess is not None
        # Two configured greenhouse rows beat the unconfigured embed row.
        assert guess.config == {"board_token": "stripe"}

    def test_incomplete_rows_are_skipped_not_fatal(self) -> None:
        companies = aggregate_listings(self._rows())
        assert all(c.normalized for c in companies)
        assert "ghost" not in {c.normalized for c in companies}

    def test_results_are_ordered_by_listing_volume(self) -> None:
        companies = aggregate_listings(self._rows())
        assert [c.normalized for c in companies] == ["stripe", "notion"]

    def test_empty_input_returns_empty_list(self) -> None:
        assert aggregate_listings([]) == []
