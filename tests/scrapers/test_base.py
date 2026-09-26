"""Level 2 - the guarantees the base class makes for *every* adapter.

These are the invariants that must not depend on which provider is in play:
per-record containment, dedup, page limits, health-record construction, and
`safe_fetch` never letting a scraper failure escape (PRD §7, §16).
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any, ClassVar

import pytest
from tests.conftest import make_company

from jobmonitor.errors import (
    AccessBlocked,
    InvalidJobError,
    ParseError,
    ProviderConfigError,
    RetryBudgetExhausted,
)
from jobmonitor.models.health import ScraperStatus
from jobmonitor.models.job import Job
from jobmonitor.scrapers import registered_providers, source_class_for
from jobmonitor.scrapers.base import (
    MAX_PAGES,
    JobSource,
    build_source,
    build_sources,
    register,
    safe_fetch,
)

pytestmark = pytest.mark.scrapers


class ListSource(JobSource):
    """A JobSource over in-memory pages, so base-class behaviour can be isolated."""

    provider: ClassVar[str] = "test-list"
    required_config: ClassVar[tuple[str, ...]] = ()

    def __init__(self, company: Any, pages: Sequence[Sequence[Any]], **kwargs: Any) -> None:
        super().__init__(company, **kwargs)
        self.pages = pages
        self.pages_yielded = 0

    def fetch_pages(self) -> Iterator[Sequence[Any]]:
        for page in self.pages:
            self.pages_yielded += 1
            yield page

    def normalize(self, raw_job: Any) -> Job:
        return Job(
            company=self.company.company,
            title=str(raw_job.get("title", "")),
            url=str(raw_job.get("url", "")),
            source=self.provider,
            external_id=raw_job.get("id"),
        )


def good(identifier: str, title: str = "Software Engineer Intern") -> dict[str, Any]:
    return {"id": identifier, "title": title, "url": f"https://x.example.com/j/{identifier}"}


class TestPerRecordContainment:
    def test_one_invalid_record_does_not_lose_the_others(self) -> None:
        source = ListSource(
            make_company(provider="test-list"),
            [[good("1"), {"id": "2", "title": "", "url": ""}, good("3")]],
        )
        result = source.fetch()
        assert [job.external_id for job in result.jobs] == ["1", "3"]
        assert result.malformed == 1

    def test_shape_surprises_are_contained_too(self) -> None:
        # A bare string where an object was expected raises AttributeError in
        # normalize(); it must be handled like an InvalidJobError.
        source = ListSource(make_company(provider="test-list"), [[good("1"), "oops", good("2")]])
        result = source.fetch()
        assert len(result.jobs) == 2
        assert result.malformed == 1

    def test_all_records_invalid_yields_no_jobs_but_does_not_raise(self) -> None:
        source = ListSource(make_company(provider="test-list"), [[{"title": "", "url": ""}]])
        result = source.fetch()
        assert result.jobs == []
        assert result.malformed == 1


class TestDeduplication:
    def test_duplicate_records_from_the_source_are_collapsed(self) -> None:
        source = ListSource(make_company(provider="test-list"), [[good("1"), good("1"), good("2")]])
        assert [job.external_id for job in source.fetch().jobs] == ["1", "2"]

    def test_duplicates_across_pages_are_collapsed(self) -> None:
        source = ListSource(make_company(provider="test-list"), [[good("1")], [good("1")]])
        assert len(source.fetch().jobs) == 1


class TestPagination:
    def test_all_pages_are_consumed(self) -> None:
        source = ListSource(
            make_company(provider="test-list"), [[good("1")], [good("2")], [good("3")]]
        )
        result = source.fetch()
        assert len(result.jobs) == 3
        assert result.pages == 3

    def test_empty_pages_are_tolerated(self) -> None:
        source = ListSource(make_company(provider="test-list"), [[], [good("1")], []])
        assert len(source.fetch().jobs) == 1

    def test_runaway_pagination_is_capped(self) -> None:
        pages = [[good(str(i))] for i in range(MAX_PAGES + 25)]
        source = ListSource(make_company(provider="test-list"), pages)
        result = source.fetch()
        assert result.pages == MAX_PAGES
        assert len(result.jobs) == MAX_PAGES


class TestConfigValidation:
    def test_missing_required_config_key_is_rejected_at_construction(self) -> None:
        company = make_company(provider="greenhouse", config={})
        with pytest.raises(ProviderConfigError, match="board_token"):
            build_source(company)

    def test_blank_required_config_value_is_rejected(self) -> None:
        company = make_company(provider="greenhouse", config={"board_token": ""})
        with pytest.raises(ProviderConfigError, match="board_token"):
            build_source(company)

    def test_unknown_provider_is_reported_with_the_known_list(self) -> None:
        company = make_company(provider="nonexistent-ats", config={"x": 1})
        with pytest.raises(ProviderConfigError, match="no adapter registered"):
            build_source(company)


class TestRegistry:
    def test_every_provider_in_the_shipped_registry_has_an_adapter(self) -> None:
        from jobmonitor.models.company import load_default_registry

        for company in load_default_registry().pollable():
            assert source_class_for(company.provider) is not None, company.company

    def test_registered_providers_are_sorted_and_include_all_ats_adapters(self) -> None:
        providers = registered_providers()
        assert list(providers) == sorted(providers)
        for expected in ("greenhouse", "lever", "ashby", "workday", "smartrecruiters"):
            assert expected in providers

    def test_registering_a_provider_key_twice_is_an_error(self) -> None:
        class Duplicate(ListSource):
            provider: ClassVar[str] = "greenhouse"

        with pytest.raises(ValueError, match="already registered"):
            register(Duplicate)

    def test_registering_without_a_provider_key_is_an_error(self) -> None:
        class Anonymous(ListSource):
            provider: ClassVar[str] = ""

        with pytest.raises(ValueError, match="must define a provider key"):
            register(Anonymous)


class TestBuildSources:
    def test_broken_entries_become_health_records_not_exceptions(self) -> None:
        companies = [
            make_company("Good Co", provider="greenhouse"),
            make_company("Bad Co", provider="greenhouse", config={}),
            make_company("Unknown Co", provider="does-not-exist", config={"a": 1}),
        ]
        sources, problems = build_sources(companies)
        assert [s.company.company for s in sources] == ["Good Co"]
        assert {p.company for p in problems} == {"Bad Co", "Unknown Co"}
        assert all(p.status is ScraperStatus.FAILED for p in problems)


class TestSafeFetch:
    def _explode(self, exc: BaseException) -> ListSource:
        class Exploding(ListSource):
            provider: ClassVar[str] = "test-list"

            def fetch_pages(self) -> Iterator[Sequence[Any]]:
                raise exc

        return Exploding(make_company(provider="test-list"), [])

    def test_success_produces_a_success_record_with_counts(self) -> None:
        source = ListSource(make_company(provider="test-list"), [[good("1"), good("2")]])
        result = safe_fetch(source)
        assert result.health is not None
        assert result.health.status is ScraperStatus.SUCCESS
        assert result.health.jobs_found == 2
        assert result.health.jobs_malformed == 0
        assert result.ok

    def test_no_postings_is_empty_not_failure(self) -> None:
        # PRD §7: empty internship results must not imply scraper failure.
        result = safe_fetch(ListSource(make_company(provider="test-list"), [[]]))
        assert result.health is not None
        assert result.health.status is ScraperStatus.EMPTY
        assert result.health.ok

    def test_partial_parse_is_degraded_but_still_ok(self) -> None:
        source = ListSource(
            make_company(provider="test-list"), [[good("1"), {"title": "", "url": ""}]]
        )
        result = safe_fetch(source)
        assert result.health is not None
        assert result.health.status is ScraperStatus.DEGRADED
        assert result.health.jobs_found == 1
        assert result.health.jobs_malformed == 1
        assert result.health.ok

    @pytest.mark.parametrize(
        ("exc", "expected"),
        [
            (ParseError("bad payload"), ScraperStatus.FAILED),
            (
                RetryBudgetExhausted("gave up", attempts=3, last_error=OSError()),
                ScraperStatus.FAILED,
            ),
            (InvalidJobError("nope"), ScraperStatus.FAILED),
            (AccessBlocked("403"), ScraperStatus.BLOCKED),
        ],
    )
    def test_known_failures_become_health_records(
        self, exc: BaseException, expected: ScraperStatus
    ) -> None:
        result = safe_fetch(self._explode(exc))
        assert result.health is not None
        assert result.health.status is expected
        assert result.health.error_type == type(exc).__name__
        assert result.health.error
        assert not result.ok

    def test_an_unexpected_bug_in_an_adapter_is_also_contained(self) -> None:
        # The catch-all matters: a KeyError in someone's parser must not abort a poll.
        result = safe_fetch(self._explode(KeyError("surprise")))
        assert result.health is not None
        assert result.health.status is ScraperStatus.FAILED
        assert result.health.error_type == "KeyError"

    def test_keyboard_interrupt_is_not_swallowed(self) -> None:
        with pytest.raises(KeyboardInterrupt):
            safe_fetch(self._explode(KeyboardInterrupt()))

    def test_duration_is_recorded(self) -> None:
        ticks = iter([0.0, 0.25])
        result = safe_fetch(
            ListSource(make_company(provider="test-list"), [[good("1")]]),
            clock=lambda: next(ticks),
        )
        assert result.health is not None
        assert result.health.duration_ms == 250

    def test_healthcheck_uses_the_same_path(self) -> None:
        health = ListSource(make_company(provider="test-list"), [[good("1")]]).healthcheck()
        assert health.status is ScraperStatus.SUCCESS
        assert health.company == "TestCo"
