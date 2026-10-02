#!/usr/bin/env python3
"""Validate every registry entry against its **live** configured source.

This is the step PRD §31/§32 asks for, and the one thing this build could not run
itself: the sandbox's egress policy answers 403 to CONNECT for every ATS host
(BLOCKERS.md BLK-001). The script is complete and ready - run it anywhere with
ordinary outbound HTTPS:

    python scripts/validate_companies.py                 # probe everything
    python scripts/validate_companies.py --write         # ...and update companies.json
    python scripts/validate_companies.py --provider ashby --limit 5
    python scripts/validate_companies.py --json report.json

For each company it performs one real fetch through the company's own adapter and
maps the outcome to a support status:

    jobs parsed                 -> supported
    fetched, zero postings      -> supported   (an empty board is normal, PRD §7)
    degraded (records missing)  -> partial
    401/403/bot wall            -> blocked
    404 / unparseable payload   -> research-needed
    429 / 5xx / timeout         -> INCONCLUSIVE: status unchanged, not recorded
    community-feed fallback     -> partial     (never promoted above partial)

Why transient failures are inconclusive: a rate limit says nothing about whether
the configuration is right. Mapping it to research-needed would stop the company
being polled at all - during the first live run Microsoft answered 429 after a
burst of development traffic, and would have been dropped.

With ``--write`` the verdicts go to ``data/validation.json`` (observed facts),
never into ``companies.json`` directly: the registry is *generated* from the
editorial universe plus those observations by ``build_company_registry.py``, which
this script then re-runs. Writing the registry in place would make the build's
``--check`` - and so ``make verify`` - fail the moment validation ran.

``--events`` probes every company's *event sources* instead (events pages, Luma
calendars, Avature events portals, sitemaps) and, with ``--write``, records each
one's verdict and a sample of current events under ``event_sources`` in the same
file. An event source with nothing listed today is fine (events are seasonal);
one that fails is reported.

Companies whose provider has no adapter (recorded as ``blocked``, e.g. Bloomberg)
are listed and skipped. The probe is deliberately gentle: sequential, with a pause
between companies, and it never retries a 403.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from jobmonitor.config import Settings  # noqa: E402
from jobmonitor.http import HttpClient  # noqa: E402
from jobmonitor.models.company import (  # noqa: E402
    Company,
    CompanyRegistry,
    SupportStatus,
    default_registry_path,
)
from jobmonitor.models.health import ScraperStatus  # noqa: E402
from jobmonitor.scrapers import build_source, registered_providers, safe_fetch  # noqa: E402

VALIDATION_PATH = REPO_ROOT / "data" / "validation.json"

#: error_type values that say "try again later", not "this configuration is wrong".
TRANSIENT_ERRORS = frozenset(
    {"RetryBudgetExhausted", "RateLimited", "ServerError", "NetworkError", "TimeoutError"}
)

#: Health status -> registry support status.
STATUS_MAP = {
    ScraperStatus.SUCCESS: SupportStatus.SUPPORTED,
    ScraperStatus.EMPTY: SupportStatus.SUPPORTED,
    ScraperStatus.DEGRADED: SupportStatus.PARTIAL,
    ScraperStatus.BLOCKED: SupportStatus.BLOCKED,
    ScraperStatus.FAILED: SupportStatus.RESEARCH_NEEDED,
}

#: Providers that can never be promoted past `partial`, whatever they return,
#: because they are second-hand sources (PRD §4.3).
SECOND_HAND_PROVIDERS = frozenset({"simplify_fallback"})


@dataclass
class Outcome:
    company: str
    provider: str
    previous: SupportStatus
    resolved: SupportStatus
    jobs: int
    malformed: int
    duration_ms: int
    error: str | None
    #: True when the failure was transient: the verdict is withheld.
    inconclusive: bool = False

    @property
    def changed(self) -> bool:
        return not self.inconclusive and self.previous is not self.resolved

    def to_dict(self) -> dict[str, object]:
        return {
            "company": self.company,
            "provider": self.provider,
            "previous_status": self.previous.value,
            "resolved_status": self.resolved.value,
            "jobs": self.jobs,
            "malformed": self.malformed,
            "duration_ms": self.duration_ms,
            "error": self.error,
            "inconclusive": self.inconclusive,
        }


def probe(company: Company, client: HttpClient) -> Outcome:
    result = safe_fetch(build_source(company, client))
    health = result.health
    assert health is not None
    resolved = STATUS_MAP.get(health.status, SupportStatus.RESEARCH_NEEDED)
    inconclusive = (
        health.status is ScraperStatus.FAILED and (health.error_type or "") in TRANSIENT_ERRORS
    ) or (
        # Degraded *only* because a later search term failed transiently (Microsoft's
        # "summer analyst" term hit a rate limit): that says nothing about the config.
        health.status is ScraperStatus.DEGRADED
        and bool(result.partial_failures)
        and not result.truncated
        and not result.malformed
        and all(
            any(kind in failure for kind in TRANSIENT_ERRORS) for failure in result.partial_failures
        )
    )
    if inconclusive:
        resolved = company.support_status
    if company.provider in SECOND_HAND_PROVIDERS and resolved is SupportStatus.SUPPORTED:
        resolved = SupportStatus.PARTIAL
    return Outcome(
        company=company.company,
        provider=company.provider,
        previous=company.support_status,
        resolved=resolved,
        jobs=len(result.jobs),
        malformed=result.malformed,
        duration_ms=health.duration_ms,
        error=health.error,
        inconclusive=inconclusive,
    )


@dataclass
class EventOutcome:
    key: str
    company: str
    provider: str
    category: str
    status: str
    events: int
    sample: list[str]
    error: str | None
    inconclusive: bool = False

    @property
    def ok(self) -> bool:
        return self.status in {ScraperStatus.SUCCESS.value, ScraperStatus.EMPTY.value}


def probe_event_source(target: Company, client: HttpClient) -> EventOutcome:
    result = safe_fetch(build_source(target, client))
    health = result.health
    assert health is not None
    return EventOutcome(
        key=target.key,
        company=target.company,
        provider=target.health_provider,
        category=target.event_category.value if target.event_category else "",
        status=health.status.value,
        events=len(result.jobs),
        sample=[job.title for job in result.jobs[:3]],
        error=health.error,
        inconclusive=(
            health.status is ScraperStatus.FAILED and (health.error_type or "") in TRANSIENT_ERRORS
        ),
    )


def validate_events(registry: CompanyRegistry, args: argparse.Namespace) -> int:
    targets = [source for company in registry for source in company.sources()[1:]]
    if args.provider:
        targets = [t for t in targets if t.provider == args.provider]
    if args.company:
        match = registry.get(args.company)
        targets = [t for t in targets if match and t.company == match.company]
    if args.limit:
        targets = targets[: args.limit]
    if not targets:
        print("no event sources matched the filters", file=sys.stderr)
        return 2

    client = HttpClient(Settings.from_env().http)
    today = datetime.now(UTC).date().isoformat()
    outcomes: list[EventOutcome] = []
    print(f"probing {len(targets)} event sources (delay {args.delay}s)\n")
    for index, target in enumerate(targets, start=1):
        outcome = probe_event_source(target, client)
        outcomes.append(outcome)
        marker = "?" if outcome.inconclusive else (" " if outcome.ok else "!")
        print(
            f"{marker} [{index:>2}/{len(targets)}] {target.company:<20} {outcome.provider:<22} "
            f"{outcome.status:<9} events={outcome.events:<3} "
            f"{outcome.error or '; '.join(outcome.sample)}"[:170]
        )
        if args.delay and index < len(targets):
            time.sleep(args.delay)

    failed = [o for o in outcomes if not o.ok and not o.inconclusive]
    print(
        f"\n{len(outcomes) - len(failed)}/{len(outcomes)} event sources answered; validated {today}"
    )
    if args.json:
        args.json.write_text(
            json.dumps({"validated_on": today, "outcomes": [vars(o) for o in outcomes]}, indent=2)
            + "\n",
            encoding="utf-8",
        )
    if args.write:
        data = _load_validation()
        recorded = data.setdefault("event_sources", {})
        for outcome in outcomes:
            if outcome.inconclusive:
                continue
            recorded[outcome.key] = {
                "category": outcome.category,
                "status": outcome.status,
                "validated_on": today,
                "events": outcome.events,
                "sample": outcome.sample,
                "error": outcome.error,
            }
        data["event_sources"] = dict(sorted(recorded.items()))
        _write_validation(data)
        print(f"{VALIDATION_PATH.relative_to(REPO_ROOT)} updated")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, default=default_registry_path())
    parser.add_argument("--provider", help="only companies using this provider")
    parser.add_argument("--company", help="only this company (exact name or alias)")
    parser.add_argument("--limit", type=int, help="stop after N companies")
    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="seconds to pause between companies (default 1.0; be a good citizen)",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="record verdicts in data/validation.json and regenerate companies.json",
    )
    parser.add_argument("--json", type=Path, help="write a machine-readable report here")
    parser.add_argument(
        "--events", action="store_true", help="probe event sources instead of job boards"
    )
    args = parser.parse_args(argv)

    registry = CompanyRegistry.load(args.registry)
    if args.events:
        return validate_events(registry, args)
    adapters = set(registered_providers())
    skipped = [c for c in registry if c.provider not in adapters]
    targets = [c for c in registry if c.provider in adapters]
    for company in skipped:
        print(f"  skipped {company.company:<32} no adapter ({company.support_status.value})")
    if args.provider:
        targets = [c for c in targets if c.provider == args.provider]
    if args.company:
        match = registry.get(args.company)
        targets = [match] if match else []
    if args.limit:
        targets = targets[: args.limit]

    if not targets:
        print("no companies matched the filters", file=sys.stderr)
        return 2

    settings = Settings.from_env()
    client = HttpClient(settings.http)
    today = datetime.now(UTC).date().isoformat()

    outcomes: list[Outcome] = []

    print(f"probing {len(targets)} companies (delay {args.delay}s)\n")
    for index, company in enumerate(targets, start=1):
        outcome = probe(company, client)
        outcomes.append(outcome)
        marker = "?" if outcome.inconclusive else ("!" if outcome.changed else " ")
        print(
            f"{marker} [{index:>3}/{len(targets)}] {company.company:<32} "
            f"{company.provider:<18} {outcome.resolved.value:<16} "
            f"jobs={outcome.jobs:<4} {outcome.error or ''}"[:160]
        )
        if args.delay and index < len(targets):
            time.sleep(args.delay)

    by_status: dict[str, int] = {}
    for outcome in outcomes:
        key = "inconclusive" if outcome.inconclusive else outcome.resolved.value
        by_status[key] = by_status.get(key, 0) + 1
    changed = [o for o in outcomes if o.changed]
    inconclusive = [o for o in outcomes if o.inconclusive]

    print(f"\nresults: {by_status}")
    print(f"validated on: {today}")
    if changed:
        print(f"\n{len(changed)} status change(s):")
        for outcome in changed:
            print(
                f"  {outcome.company:<32} {outcome.previous.value} -> {outcome.resolved.value}"
                f"  {outcome.error or ''}"[:160]
            )
    else:
        print("\nno status changes")
    if inconclusive:
        print(f"\n{len(inconclusive)} inconclusive (transient failure; status left as it was):")
        for outcome in inconclusive:
            print(f"  {outcome.company:<32} {outcome.error or ''}"[:160])

    if args.json:
        args.json.write_text(
            json.dumps(
                {
                    "validated_on": today,
                    "by_status": by_status,
                    "outcomes": [o.to_dict() for o in outcomes],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"\nreport written to {args.json}")

    if args.write:
        record_verdicts(outcomes, today)
        import subprocess

        subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "build_company_registry.py")], check=True
        )
        print(f"\n{VALIDATION_PATH.relative_to(REPO_ROOT)} and companies.json updated")
        print("Remember to regenerate the coverage report: make coverage-report")

    # Non-zero when anything is now unsupported, so CI can notice a dead slug.
    broken = sum(
        1 for o in outcomes if o.resolved in {SupportStatus.BLOCKED, SupportStatus.RESEARCH_NEEDED}
    )
    return 1 if broken else 0


def record_verdicts(outcomes: list[Outcome], today: str) -> None:
    """Merge conclusive verdicts into data/validation.json.

    Merged, not replaced, so validating one provider does not erase the others;
    inconclusive outcomes are not written, so a rate limit never overwrites the
    last real verdict.
    """
    data = _load_validation()
    existing: dict[str, dict[str, object]] = data.get("companies", {})
    for outcome in outcomes:
        if outcome.inconclusive:
            continue
        existing[outcome.company] = {
            "status": outcome.resolved.value,
            "validated_on": today,
            "jobs": outcome.jobs,
            "error": outcome.error,
        }
    data["companies"] = dict(sorted(existing.items()))
    _write_validation(data)


def _load_validation() -> dict[str, Any]:
    if VALIDATION_PATH.exists():
        loaded: dict[str, Any] = json.loads(VALIDATION_PATH.read_text(encoding="utf-8"))
        return loaded
    return {}


def _write_validation(data: dict[str, Any]) -> None:
    """Write both sections, so validating jobs never erases event verdicts or back."""
    out = {
        "_comment": [
            "Observed, not editorial: the last conclusive live verdict per company,",
            "written by scripts/validate_companies.py --write and merged into",
            "companies.json by scripts/build_company_registry.py. event_sources holds",
            "the same for each company's event sources (--events).",
        ],
        "companies": data.get("companies", {}),
    }
    if data.get("event_sources"):
        out["event_sources"] = data["event_sources"]
    VALIDATION_PATH.write_text(
        json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    sys.exit(main())
