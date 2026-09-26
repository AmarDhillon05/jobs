"""The monitored-company registry model.

A :class:`Company` is a *monitoring target*: a canonical name plus everything
needed to ask exactly one source for its current job postings.

``support_status`` is deliberately evidence-based. Nothing is marked
``supported`` by hand; ``scripts/validate_companies.py`` probes the configured
source and writes the status back (PRD §32: "Do not claim a company is supported
unless its configured source passes the appropriate scraper test").
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class Priority(StrEnum):
    """Monitoring/notification tier - not a public ranking (PRD §3)."""

    HIGH = "high"
    MEDIUM = "medium"
    EXPERIMENTAL = "experimental"


class SupportStatus(StrEnum):
    SUPPORTED = "supported"
    PARTIAL = "partial"
    BLOCKED = "blocked"
    NEEDS_BROWSER = "needs-browser"
    UNSUPPORTED = "unsupported"
    RESEARCH_NEEDED = "research-needed"


#: Statuses whose companies are included in a normal polling run.
POLLABLE_STATUSES: frozenset[SupportStatus] = frozenset(
    {SupportStatus.SUPPORTED, SupportStatus.PARTIAL}
)


class RegistryError(ValueError):
    """Raised when companies.json cannot be interpreted."""


@dataclass(frozen=True, slots=True)
class Company:
    company: str
    careers_url: str
    industry: str
    priority: Priority
    provider: str
    provider_config: Mapping[str, Any] = field(default_factory=dict)
    source_discovered_from: tuple[str, ...] = ()
    support_status: SupportStatus = SupportStatus.RESEARCH_NEEDED
    notes: str = ""
    aliases: tuple[str, ...] = ()
    #: ISO-8601 date of the last successful live probe, when one has happened.
    last_validated: str | None = None

    def __post_init__(self) -> None:
        # Coerce the string enums so direct construction with plain strings
        # (tests, ad-hoc scripts, JSON round-trips) behaves like from_dict.
        if not isinstance(self.priority, Priority):
            try:
                object.__setattr__(self, "priority", Priority(str(self.priority)))
            except ValueError as exc:
                raise RegistryError(f"{self.company}: unknown priority {self.priority!r}") from exc
        if not isinstance(self.support_status, SupportStatus):
            try:
                object.__setattr__(self, "support_status", SupportStatus(str(self.support_status)))
            except ValueError as exc:
                raise RegistryError(
                    f"{self.company}: unknown support_status {self.support_status!r}"
                ) from exc
        if not self.company.strip():
            raise RegistryError("company name must not be empty")
        if not self.provider.strip():
            raise RegistryError(f"{self.company}: provider must not be empty")
        if not self.careers_url.startswith(("http://", "https://")):
            raise RegistryError(
                f"{self.company}: careers_url must be absolute http(s), got {self.careers_url!r}"
            )

    @property
    def key(self) -> str:
        """Stable identifier used in health records and log lines."""
        return f"{self.company}::{self.provider}"

    @property
    def is_pollable(self) -> bool:
        return self.support_status in POLLABLE_STATUSES

    # ------------------------------------------------------------ (de)serial
    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Company:
        try:
            name = str(data["company"])
            provider = str(data["provider"])
            careers_url = str(data["careers_url"])
        except KeyError as exc:
            raise RegistryError(f"company entry missing required key {exc}") from exc

        raw_priority = str(data.get("priority", "medium"))
        try:
            priority = Priority(raw_priority)
        except ValueError as exc:
            raise RegistryError(f"{name}: unknown priority {raw_priority!r}") from exc

        raw_status = str(data.get("support_status", SupportStatus.RESEARCH_NEEDED.value))
        try:
            status = SupportStatus(raw_status)
        except ValueError as exc:
            raise RegistryError(f"{name}: unknown support_status {raw_status!r}") from exc

        return cls(
            company=name,
            careers_url=careers_url,
            industry=str(data.get("industry", "unknown")),
            priority=priority,
            provider=provider,
            provider_config=dict(data.get("provider_config") or {}),
            source_discovered_from=tuple(data.get("source_discovered_from") or ()),
            support_status=status,
            notes=str(data.get("notes", "")),
            aliases=tuple(data.get("aliases") or ()),
            last_validated=data.get("last_validated") or None,
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "company": self.company,
            "careers_url": self.careers_url,
            "industry": self.industry,
            "priority": self.priority.value,
            "provider": self.provider,
            "provider_config": dict(self.provider_config),
            "source_discovered_from": list(self.source_discovered_from),
            "support_status": self.support_status.value,
            "notes": self.notes,
        }
        if self.aliases:
            out["aliases"] = list(self.aliases)
        if self.last_validated:
            out["last_validated"] = self.last_validated
        return out


@dataclass(frozen=True, slots=True)
class CompanyRegistry:
    """An ordered, name-unique collection of monitoring targets."""

    companies: tuple[Company, ...]

    def __post_init__(self) -> None:
        seen: dict[str, str] = {}
        for company in self.companies:
            folded = company.company.casefold()
            if folded in seen:
                raise RegistryError(f"duplicate company entry: {company.company!r}")
            seen[folded] = company.company

    def __iter__(self) -> Iterator[Company]:
        return iter(self.companies)

    def __len__(self) -> int:
        return len(self.companies)

    def get(self, name: str) -> Company | None:
        folded = name.casefold()
        for company in self.companies:
            if company.company.casefold() == folded:
                return company
            if any(alias.casefold() == folded for alias in company.aliases):
                return company
        return None

    def pollable(self) -> tuple[Company, ...]:
        return tuple(c for c in self.companies if c.is_pollable)

    def by_provider(self, provider: str) -> tuple[Company, ...]:
        return tuple(c for c in self.companies if c.provider == provider)

    def with_status(self, status: SupportStatus) -> tuple[Company, ...]:
        return tuple(c for c in self.companies if c.support_status is status)

    def shard(self, size: int) -> tuple[tuple[Company, ...], ...]:
        """Split pollable companies into fixed-size batches for the queue.

        Sharding is what keeps a single Lambda invocation well inside its
        timeout and lets one company's failure only affect its own shard.
        """
        if size < 1:
            raise RegistryError(f"shard size must be >= 1, got {size}")
        targets = self.pollable()
        return tuple(targets[i : i + size] for i in range(0, len(targets), size))

    # ------------------------------------------------------------ (de)serial
    @classmethod
    def from_list(cls, entries: Iterable[Mapping[str, Any]]) -> CompanyRegistry:
        return cls(tuple(Company.from_dict(entry) for entry in entries))

    @classmethod
    def load(cls, path: str | Path) -> CompanyRegistry:
        raw = Path(path).read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, list):
            raise RegistryError(f"{path}: expected a JSON list of company objects")
        return cls.from_list(data)

    def to_list(self) -> list[dict[str, Any]]:
        return [company.to_dict() for company in self.companies]

    def dump(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(self.to_list(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

    def counts_by_status(self) -> dict[str, int]:
        counts = {status.value: 0 for status in SupportStatus}
        for company in self.companies:
            counts[company.support_status.value] += 1
        return counts

    def counts_by_provider(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for company in self.companies:
            counts[company.provider] = counts.get(company.provider, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def default_registry_path() -> Path:
    """Repo-root ``companies.json``, resolved relative to this module."""
    return Path(__file__).resolve().parents[3] / "companies.json"


def load_default_registry(path: str | Path | None = None) -> CompanyRegistry:
    return CompanyRegistry.load(path or default_registry_path())


__all__: Sequence[str] = (
    "POLLABLE_STATUSES",
    "Company",
    "CompanyRegistry",
    "Priority",
    "RegistryError",
    "SupportStatus",
    "default_registry_path",
    "load_default_registry",
)
