"""The provider abstraction every scraper implements (PRD §6).

Subclasses supply two small pieces - how to page through the source
(:meth:`JobSource.fetch_pages`) and how to read one record
(:meth:`JobSource.normalize`). The base class owns everything that must behave
identically across all providers:

* per-record error containment - one malformed posting is skipped and counted,
  never allowed to discard the rest of the response (PRD §7);
* within-source de-duplication;
* timing, attempt counting and health-record construction;
* :func:`safe_fetch`, which turns *any* scraper explosion into a health record,
  so a single broken source can never abort a polling run (PRD §16).
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar

from jobmonitor.errors import (
    AccessBlocked,
    InvalidJobError,
    JobMonitorError,
    ParseError,
    ProviderConfigError,
)
from jobmonitor.http import HttpClient
from jobmonitor.identity import deduplicate
from jobmonitor.models.company import Company
from jobmonitor.models.health import ScraperHealth, ScraperStatus
from jobmonitor.models.job import Job

MAX_PAGES = 100
"""Hard stop on pagination, against a provider that hands out "next page" forever.

Sized from live data: NVIDIA's Workday board needs 51 pages of 20. Hitting the cap
is never silent - the fetch is marked truncated and the health record DEGRADED."""


@dataclass(slots=True)
class FetchResult:
    """What one scraper produced, plus how it went."""

    jobs: list[Job] = field(default_factory=list)
    health: ScraperHealth | None = None
    malformed: int = 0
    pages: int = 0
    #: True when the page cap stopped the fetch before the source ran out.
    truncated: bool = False
    #: Searches after the first that failed; their results are missing.
    partial_failures: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.health is None or self.health.ok


class JobSource(ABC):
    """One monitored company's postings, from one source."""

    #: Provider key, matching ``Company.provider``.
    provider: ClassVar[str] = ""
    #: Keys that must be present in ``Company.provider_config``.
    required_config: ClassVar[tuple[str, ...]] = ()

    def __init__(self, company: Company, client: HttpClient | None = None) -> None:
        self.company = company
        self.client = client or HttpClient()
        #: Filled by :meth:`search_all` when a later search term fails.
        self.partial_failures: list[str] = []
        self._validate_config()

    # ------------------------------------------------------------------- config
    def _validate_config(self) -> None:
        missing = [key for key in self.required_config if not self.config.get(key)]
        if missing:
            raise ProviderConfigError(
                f"{self.company.company}: {self.provider} adapter requires "
                f"{list(self.required_config)} in provider_config, missing {missing}"
            )

    @property
    def config(self) -> Mapping[str, Any]:
        return self.company.provider_config

    def config_str(self, key: str) -> str:
        return str(self.config[key])

    # ---------------------------------------------------------------- subclasses
    @abstractmethod
    def fetch_pages(self) -> Iterator[Sequence[Any]]:
        """Yield one sequence of raw records per page of the source.

        Implementations that are not paginated simply yield once. Implementations
        that are paginated must stop themselves; the base class also enforces
        :data:`MAX_PAGES` so no result set is silently truncated *and* no loop
        runs away (PRD §7).
        """

    @abstractmethod
    def normalize(self, raw_job: Any) -> Job:
        """Turn one raw record into a :class:`Job`.

        Raise :class:`~jobmonitor.errors.InvalidJobError` for a record that
        cannot be used; the base class skips and counts it.
        """

    def describe(self) -> str:
        """Human-readable source identity, used in logs and health records."""
        return f"{self.company.company}/{self.provider}"

    def search_all(
        self,
        queries: Sequence[str],
        search: Callable[[str], Iterator[Sequence[Any]]],
    ) -> Iterator[Sequence[Any]]:
        """Run several searches as one fetch, for adapters that must search.

        The **first** search failing fails the fetch, exactly as a single-request
        adapter would - that is how a site that is down or refusing us shows up.
        A **later** search failing keeps everything gathered so far and records
        the term, so the health record turns DEGRADED instead of one flaky extra
        term discarding every result (observed live: one of Microsoft's five
        searches failed transiently and took all 98 postings with it).
        """
        for index, query in enumerate(queries):
            try:
                yield from search(query)
            except JobMonitorError as exc:
                if index == 0:
                    raise
                self.partial_failures.append(f"{query!r}: {type(exc).__name__}: {exc}")

    # --------------------------------------------------------------- public API
    def fetch_jobs(self) -> list[Job]:
        """All currently-listed postings for this company, normalized."""
        return self.fetch().jobs

    def fetch(self) -> FetchResult:
        """:meth:`fetch_jobs` plus the counters the health record needs."""
        result = FetchResult()
        collected: list[Job] = []

        for page_number, page in enumerate(self.fetch_pages(), start=1):
            result.pages = page_number
            for raw in page:
                try:
                    collected.append(self.normalize(raw))
                except InvalidJobError:
                    result.malformed += 1
                except (KeyError, TypeError, ValueError, AttributeError):
                    # A shape surprise in one record is the same class of problem
                    # as an explicitly invalid one: skip it, keep the rest.
                    result.malformed += 1
            if page_number >= MAX_PAGES:
                # Recorded, never silent: the health record turns DEGRADED and says
                # why, so a board that outgrew the cap shows up in the health view.
                result.truncated = True
                break

        result.jobs = deduplicate(collected)
        result.partial_failures = list(self.partial_failures)
        return result

    def healthcheck(self) -> ScraperHealth:
        """Fetch once and report how it went, without raising."""
        return safe_fetch(self).health or ScraperHealth(
            company=self.company.company,
            provider=self.provider,
            status=ScraperStatus.SUCCESS,
        )


def safe_fetch(source: JobSource, *, clock: Any = time.perf_counter) -> FetchResult:
    """Run a scraper and *never* propagate its failure.

    This is the failure-isolation boundary required by PRD §16: whatever a source
    does - HTTP error, unparseable payload, a bug in its own adapter - the caller
    gets a :class:`FetchResult` carrying a health record, and the other companies
    in the run are unaffected.
    """
    started = clock()
    company = source.company.company
    provider = source.provider

    def elapsed_ms() -> int:
        return int((clock() - started) * 1000)

    try:
        result = source.fetch()
    except AccessBlocked as exc:
        return FetchResult(
            health=ScraperHealth(
                company=company,
                provider=provider,
                status=ScraperStatus.BLOCKED,
                attempts=source.client.log.attempts or 1,
                duration_ms=elapsed_ms(),
                error_type=type(exc).__name__,
                error=str(exc),
            )
        )
    except (JobMonitorError, ParseError) as exc:
        return FetchResult(
            health=ScraperHealth(
                company=company,
                provider=provider,
                status=ScraperStatus.FAILED,
                attempts=source.client.log.attempts or 1,
                duration_ms=elapsed_ms(),
                error_type=type(exc).__name__,
                error=str(exc),
            )
        )
    except Exception as exc:
        return FetchResult(
            health=ScraperHealth(
                company=company,
                provider=provider,
                status=ScraperStatus.FAILED,
                attempts=source.client.log.attempts or 1,
                duration_ms=elapsed_ms(),
                error_type=type(exc).__name__,
                error=str(exc),
            )
        )

    error: str | None = None
    if result.truncated:
        status = ScraperStatus.DEGRADED
        error = f"stopped at the {MAX_PAGES}-page cap; later postings were not read"
    elif result.partial_failures:
        status = ScraperStatus.DEGRADED
        error = (
            "some searches failed, their results are missing: "
            + "; ".join(result.partial_failures)[:400]
        )
    elif result.malformed and result.jobs:
        status = ScraperStatus.DEGRADED
    elif result.jobs:
        status = ScraperStatus.SUCCESS
    elif result.malformed:
        status = ScraperStatus.DEGRADED
    else:
        # No postings is a normal state, not a failure.
        status = ScraperStatus.EMPTY

    result.health = ScraperHealth(
        company=company,
        provider=provider,
        status=status,
        attempts=source.client.log.attempts or 1,
        duration_ms=elapsed_ms(),
        jobs_found=len(result.jobs),
        jobs_malformed=result.malformed,
        error=error,
    )
    return result


# ------------------------------------------------------------------- registry

_REGISTRY: dict[str, type[JobSource]] = {}


def register(source_class: type[JobSource]) -> type[JobSource]:
    """Class decorator: make an adapter discoverable by its provider key."""
    key = source_class.provider
    if not key:
        raise ValueError(f"{source_class.__name__} must define a provider key")
    existing = _REGISTRY.get(key)
    if existing is not None and existing is not source_class:
        raise ValueError(f"provider {key!r} already registered to {existing.__name__}")
    _REGISTRY[key] = source_class
    return source_class


def registered_providers() -> tuple[str, ...]:
    return tuple(sorted(_REGISTRY))


def source_class_for(provider: str) -> type[JobSource]:
    try:
        return _REGISTRY[provider]
    except KeyError:
        raise ProviderConfigError(
            f"no adapter registered for provider {provider!r}; "
            f"available: {list(registered_providers())}"
        ) from None


def build_source(company: Company, client: HttpClient | None = None) -> JobSource:
    """Instantiate the right adapter for a company."""
    return source_class_for(company.provider)(company, client)


def build_sources(
    companies: Iterable[Company], client: HttpClient | None = None
) -> tuple[list[JobSource], list[ScraperHealth]]:
    """Build adapters for many companies, reporting the ones that cannot be built.

    A company with a broken registry entry becomes a health record rather than an
    exception, for the same reason ``safe_fetch`` exists.
    """
    sources: list[JobSource] = []
    problems: list[ScraperHealth] = []
    for company in companies:
        try:
            sources.append(build_source(company, client))
        except (ProviderConfigError, ValueError) as exc:
            problems.append(
                ScraperHealth(
                    company=company.company,
                    provider=company.provider,
                    status=ScraperStatus.FAILED,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
            )
    return sources, problems


__all__: Sequence[str] = (
    "MAX_PAGES",
    "FetchResult",
    "JobSource",
    "build_source",
    "build_sources",
    "register",
    "registered_providers",
    "safe_fetch",
    "source_class_for",
)
