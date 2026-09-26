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
    some records unparseable    -> partial
    401/403/bot wall            -> blocked
    404 / unparseable payload   -> research-needed
    community-feed fallback     -> partial     (never promoted above partial)

With ``--write`` it records ``last_validated`` so the coverage report can state
the date the claim was last checked, and prints a diff of every status change.
It is deliberately gentle: sequential by default, with a pause between requests,
and it never retries a 403.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

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
from jobmonitor.scrapers import build_source, safe_fetch  # noqa: E402

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

    @property
    def changed(self) -> bool:
        return self.previous is not self.resolved

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
        }


def probe(company: Company, client: HttpClient) -> Outcome:
    result = safe_fetch(build_source(company, client))
    health = result.health
    assert health is not None
    resolved = STATUS_MAP.get(health.status, SupportStatus.RESEARCH_NEEDED)
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
    )


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
    parser.add_argument("--write", action="store_true", help="update companies.json in place")
    parser.add_argument("--json", type=Path, help="write a machine-readable report here")
    args = parser.parse_args(argv)

    registry = CompanyRegistry.load(args.registry)
    targets = list(registry)
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
    updated: dict[str, Company] = {}

    print(f"probing {len(targets)} companies (delay {args.delay}s)\n")
    for index, company in enumerate(targets, start=1):
        outcome = probe(company, client)
        outcomes.append(outcome)
        marker = "!" if outcome.changed else " "
        print(
            f"{marker} [{index:>3}/{len(targets)}] {company.company:<32} "
            f"{company.provider:<18} {outcome.resolved.value:<16} "
            f"jobs={outcome.jobs:<4} {outcome.error or ''}"[:160]
        )
        from dataclasses import replace

        updated[company.company] = replace(
            company, support_status=outcome.resolved, last_validated=today
        )
        if args.delay and index < len(targets):
            time.sleep(args.delay)

    by_status: dict[str, int] = {}
    for outcome in outcomes:
        by_status[outcome.resolved.value] = by_status.get(outcome.resolved.value, 0) + 1
    changed = [o for o in outcomes if o.changed]

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
        merged = CompanyRegistry(
            tuple(updated.get(company.company, company) for company in registry)
        )
        merged.dump(args.registry)
        print(f"\n{args.registry} updated")
        print("Remember to regenerate the coverage report: make coverage-report")

    # Non-zero when anything is now unsupported, so CI can notice a dead slug.
    broken = sum(
        1 for o in outcomes if o.resolved in {SupportStatus.BLOCKED, SupportStatus.RESEARCH_NEEDED}
    )
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
