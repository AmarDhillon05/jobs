#!/usr/bin/env python3
"""Build ``companies.json`` from the seed repositories + the editorial universe.

Pipeline (PRD §4.3):

1. Load the two mandated seed repositories' ``listings.json`` (active *and*
   inactive rows - a closed listing still proves the company hires interns).
2. Collapse aliases into one entry per employer (:func:`aggregate_listings`).
3. Write a compact, committed discovery snapshot so the registry is rebuildable
   and reviewable without re-downloading 13 MB of raw listings.
4. For every company in the editorial universe (``data/company_universe.json``),
   resolve its provider + adapter configuration **from real observed application
   URLs**, never by hand-guessing a slug.
5. Emit ``companies.json``.

The seed repositories are a discovery guide only. The emitted registry points at
each company's own ATS endpoint, so live detection never depends on either repo.

    python scripts/build_company_registry.py
    python scripts/build_company_registry.py --check   # fail if output would change
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from jobmonitor.discovery import (  # noqa: E402
    DiscoveredCompany,
    aggregate_listings,
    careers_url_for,
    normalize_company_name,
)
from jobmonitor.models.company import (  # noqa: E402
    Company,
    CompanyRegistry,
    EventSource,
    Priority,
    SupportStatus,
)

sys.path.insert(0, str(REPO_ROOT / "scripts"))
from fetch_seed_listings import SEED_SOURCES, load_cached  # noqa: E402

UNIVERSE_PATH = REPO_ROOT / "data" / "company_universe.json"
VALIDATION_PATH = REPO_ROOT / "data" / "validation.json"
#: Second-hand sources are never promoted past `partial`, whatever a probe says.
SECOND_HAND_PROVIDERS = frozenset({"simplify_fallback"})
SNAPSHOT_PATH = REPO_ROOT / "data" / "seed" / "discovery_snapshot.json"
REGISTRY_PATH = REPO_ROOT / "companies.json"


def load_seed_rows() -> list[dict[str, Any]]:
    """All seed listings, tagged with which repository they came from."""
    cached = load_cached()
    rows: list[dict[str, Any]] = []
    for source in SEED_SOURCES:
        for row in cached[source.key]:
            row = dict(row)
            row["source"] = source.key
            rows.append(row)
    return rows


def write_snapshot(companies: list[DiscoveredCompany]) -> dict[str, Any]:
    """Compact, committed record of what discovery found."""
    snapshot = {
        "_comment": (
            "Derived from the seed repositories by scripts/build_company_registry.py. "
            "One row per employer, best-guess provider config recovered from real "
            "application URLs. Regenerate with `make companies`."
        ),
        "seed_sources": [{"key": s.key, "label": s.label, "repo": s.repo} for s in SEED_SOURCES],
        "total_employers": len(companies),
        "employers": {},
    }
    employers: dict[str, Any] = {}
    for company in companies:
        guess = company.best_guess
        employers[company.normalized] = {
            "name": company.canonical_name,
            "listings": company.listing_count,
            "sources": list(company.sources),
            "provider": guess.provider if guess else None,
            "provider_config": dict(guess.config) if guess else {},
            "supported": bool(guess and guess.is_configured),
            "reason": (guess.reason if guess and not guess.is_configured else ""),
        }
        if len(company.observed_names) > 1:
            employers[company.normalized]["observed_names"] = list(company.observed_names)
    snapshot["employers"] = employers
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_PATH.write_text(
        json.dumps(snapshot, indent=1, ensure_ascii=False, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    return snapshot


def resolve(
    entry: dict[str, Any], discovered: dict[str, DiscoveredCompany]
) -> tuple[Company | None, str]:
    """Turn one editorial entry into a :class:`Company`, or explain why not."""
    name = entry["company"]
    aliases = tuple(entry.get("aliases") or ())
    event_sources = tuple(EventSource.from_dict(e) for e in entry.get("event_sources") or ())

    # Explicitly pinned entries (the research backlog) bypass resolution.
    if entry.get("provider"):
        return (
            Company(
                company=name,
                careers_url=entry["careers_url"],
                industry=entry["industry"],
                priority=Priority(entry["priority"]),
                provider=entry["provider"],
                provider_config=dict(entry.get("provider_config") or {}),
                source_discovered_from=tuple(entry.get("source_discovered_from") or ("curated",)),
                support_status=SupportStatus(entry.get("support_status", "research-needed")),
                notes=entry.get("notes", ""),
                aliases=aliases,
                event_sources=event_sources,
            ),
            "pinned",
        )

    keys = [normalize_company_name(name), *(normalize_company_name(a) for a in aliases)]
    match: DiscoveredCompany | None = None
    for key in keys:
        candidate = discovered.get(key)
        if candidate and candidate.best_guess and candidate.best_guess.is_configured:
            match = candidate
            break
    if match is None:
        return None, f"no seed listing with a configured provider for {name!r} (keys={keys})"

    guess = match.best_guess
    assert guess is not None
    careers_url = careers_url_for(guess.provider, guess.config)
    if careers_url is None:
        return None, f"{name}: cannot build a careers URL for {guess.provider}/{guess.config}"

    return (
        Company(
            company=name,
            careers_url=careers_url,
            industry=entry["industry"],
            priority=Priority(entry["priority"]),
            provider=guess.provider,
            # The seed URLs decide *where* a company posts; an editorial override
            # may only tune *how* it is read (e.g. RTX's search terms).
            provider_config={**guess.config, **(entry.get("provider_config_overrides") or {})},
            source_discovered_from=tuple(match.sources),
            # Promoted to `supported` only by tests/scrapers/test_registry_configs.py,
            # which checks every entry against its adapter. See COMPANY_COVERAGE.md.
            support_status=SupportStatus.SUPPORTED,
            notes=(
                f"config recovered from {match.listing_count} observed posting URL(s); "
                f"seed name(s): {', '.join(match.observed_names)}"
                + (f". {entry['notes']}" if entry.get("notes") else "")
            ),
            aliases=aliases,
            event_sources=event_sources,
        ),
        "resolved",
    )


def build() -> tuple[CompanyRegistry, list[str]]:
    rows = load_seed_rows()
    discovered_list = aggregate_listings(rows)
    write_snapshot(discovered_list)
    discovered = {c.normalized: c for c in discovered_list}

    universe = json.loads(UNIVERSE_PATH.read_text(encoding="utf-8"))
    entries = [*universe.get("monitored", []), *universe.get("backlog", [])]

    companies: list[Company] = []
    problems: list[str] = []
    for entry in entries:
        company, why = resolve(entry, discovered)
        if company is None:
            problems.append(why)
        else:
            companies.append(company)

    companies = apply_validation(companies)
    companies.sort(key=lambda c: c.company.casefold())
    return CompanyRegistry(tuple(companies)), problems


def apply_validation(companies: list[Company]) -> list[Company]:
    """Overlay the last conclusive live verdict (data/validation.json), if any."""
    if not VALIDATION_PATH.exists():
        return companies
    from dataclasses import replace

    verdicts = json.loads(VALIDATION_PATH.read_text(encoding="utf-8")).get("companies", {})
    applied: list[Company] = []
    for company in companies:
        verdict = verdicts.get(company.company)
        if not verdict:
            applied.append(company)
            continue
        status = SupportStatus(verdict["status"])
        if company.provider in SECOND_HAND_PROVIDERS and status is SupportStatus.SUPPORTED:
            status = SupportStatus.PARTIAL
        applied.append(
            replace(company, support_status=status, last_validated=verdict["validated_on"])
        )
    return applied


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="exit non-zero if companies.json is stale"
    )
    args = parser.parse_args(argv)

    registry, problems = build()
    payload = json.dumps(registry.to_list(), indent=2, ensure_ascii=False) + "\n"

    if args.check:
        current = REGISTRY_PATH.read_text(encoding="utf-8") if REGISTRY_PATH.exists() else ""
        if current != payload:
            print("companies.json is stale - run `make companies`", file=sys.stderr)
            return 1
        print("companies.json is up to date")
        return 0

    REGISTRY_PATH.write_text(payload, encoding="utf-8")

    print(f"wrote {REGISTRY_PATH.relative_to(REPO_ROOT)} with {len(registry)} companies")
    print(f"  pollable:  {len(registry.pollable())}")
    print(f"  by status: {registry.counts_by_status()}")
    print(f"  by provider: {registry.counts_by_provider()}")
    if problems:
        print(f"\n{len(problems)} editorial entries could not be resolved:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
