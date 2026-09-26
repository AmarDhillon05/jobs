"""Messages that cross a queue or event boundary.

Everything here is JSON round-trippable and version-tagged, because these
payloads are written by one Lambda and read by another - possibly a deploy apart.
Unknown fields are ignored and missing optional fields default, so a coordinator
and a worker at different versions degrade instead of failing.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from jobmonitor.models.company import Company, CompanyRegistry

SCHEMA_VERSION = 1


def utcnow() -> datetime:
    return datetime.now(UTC)


class MessageError(ValueError):
    """A payload could not be interpreted."""


@dataclass(frozen=True, slots=True)
class ScheduledPollEvent:
    """What the EventBridge schedule delivers to the coordinator.

    Mirrors the EventBridge event envelope, so the local injector
    (``scripts/local_poll.py``) sends a byte-identical payload to what AWS would.
    """

    poll_id: str
    scheduled_at: datetime = field(default_factory=utcnow)
    #: Optional narrowing, for manual runs: only these companies.
    only_companies: tuple[str, ...] = ()
    #: Optional narrowing: only this provider.
    only_provider: str | None = None
    dry_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "poll_id": self.poll_id,
            "scheduled_at": self.scheduled_at.isoformat(),
            "dry_run": self.dry_run,
        }
        if self.only_companies:
            payload["only_companies"] = list(self.only_companies)
        if self.only_provider:
            payload["only_provider"] = self.only_provider
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> ScheduledPollEvent:
        data = data or {}
        # EventBridge wraps a custom payload in `detail`; accept either shape.
        detail = data.get("detail") if isinstance(data.get("detail"), Mapping) else data
        assert isinstance(detail, Mapping)
        raw_time = detail.get("scheduled_at") or data.get("time")
        try:
            scheduled_at = (
                datetime.fromisoformat(str(raw_time).replace("Z", "+00:00"))
                if raw_time
                else utcnow()
            )
        except ValueError:
            scheduled_at = utcnow()
        if scheduled_at.tzinfo is None:
            scheduled_at = scheduled_at.replace(tzinfo=UTC)
        return cls(
            poll_id=str(detail.get("poll_id") or data.get("id") or scheduled_at.isoformat()),
            scheduled_at=scheduled_at,
            only_companies=tuple(str(c) for c in (detail.get("only_companies") or ())),
            only_provider=(str(detail["only_provider"]) if detail.get("only_provider") else None),
            dry_run=bool(detail.get("dry_run", False)),
        )


@dataclass(frozen=True, slots=True)
class ScrapeTask:
    """One SQS message: a shard of companies for one worker invocation.

    Companies travel *inlined* rather than as names the worker looks up. It costs
    a few KB per message and removes a whole failure mode: the worker cannot
    disagree with the coordinator about what a company's configuration is, even
    mid-deploy.
    """

    poll_id: str
    shard_index: int
    shard_count: int
    companies: tuple[Company, ...]
    dispatched_at: datetime = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        if not self.companies:
            raise MessageError("a scrape task must carry at least one company")

    @property
    def company_names(self) -> tuple[str, ...]:
        return tuple(company.company for company in self.companies)

    @property
    def message_group_id(self) -> str:
        return f"{self.poll_id}-{self.shard_index}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "poll_id": self.poll_id,
            "shard_index": self.shard_index,
            "shard_count": self.shard_count,
            "dispatched_at": self.dispatched_at.isoformat(),
            "companies": [company.to_dict() for company in self.companies],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ScrapeTask:
        raw = data.get("companies")
        if not isinstance(raw, list) or not raw:
            raise MessageError("scrape task payload has no companies")
        try:
            registry = CompanyRegistry.from_list(raw)
        except Exception as exc:
            raise MessageError(f"scrape task carries an unusable company: {exc}") from exc
        dispatched = data.get("dispatched_at")
        try:
            dispatched_at = (
                datetime.fromisoformat(str(dispatched).replace("Z", "+00:00"))
                if dispatched
                else utcnow()
            )
        except ValueError:
            dispatched_at = utcnow()
        if dispatched_at.tzinfo is None:
            dispatched_at = dispatched_at.replace(tzinfo=UTC)
        return cls(
            poll_id=str(data.get("poll_id", "unknown")),
            shard_index=int(data.get("shard_index", 0) or 0),
            shard_count=int(data.get("shard_count", 1) or 1),
            companies=registry.companies,
            dispatched_at=dispatched_at,
        )

    @classmethod
    def from_json(cls, payload: str | bytes) -> ScrapeTask:
        try:
            return cls.from_dict(json.loads(payload))
        except json.JSONDecodeError as exc:
            raise MessageError(f"scrape task body is not JSON: {exc}") from exc

    @classmethod
    def shard(
        cls, registry: CompanyRegistry, *, poll_id: str, size: int, now: datetime | None = None
    ) -> list[ScrapeTask]:
        shards = registry.shard(size)
        timestamp = now or utcnow()
        return [
            cls(
                poll_id=poll_id,
                shard_index=index,
                shard_count=len(shards),
                companies=shard,
                dispatched_at=timestamp,
            )
            for index, shard in enumerate(shards)
        ]


__all__: Sequence[str] = (
    "SCHEMA_VERSION",
    "MessageError",
    "ScheduledPollEvent",
    "ScrapeTask",
)
