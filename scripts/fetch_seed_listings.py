#!/usr/bin/env python3
"""Download the two mandated seed repositories' structured listing files.

PRD §4 requires company discovery to start from:

* https://github.com/SimplifyJobs/Summer2027-Internships
* https://github.com/vanshb03/Summer2027-Internships

Both publish a machine-readable ``.github/scripts/listings.json`` that backs the
markdown table in their README, including *inactive* rows. Inactive rows matter:
a closed listing still proves the company hires interns (PRD §4.1).

Raw downloads land in ``data/seed/raw/`` which is gitignored - the repository
commits only the compact snapshot derived by ``build_company_registry.py``.

    python scripts/fetch_seed_listings.py            # download (cached)
    python scripts/fetch_seed_listings.py --force    # re-download
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = REPO_ROOT / "data" / "seed" / "raw"

USER_AGENT = "internship-job-monitor/0.1 (seed discovery; contact: you@example.com)"


@dataclass(frozen=True)
class SeedSource:
    key: str
    label: str
    repo: str
    url: str
    filename: str


SEED_SOURCES: tuple[SeedSource, ...] = (
    SeedSource(
        key="simplify",
        label="Simplify / Pitt CSC Summer 2027",
        repo="https://github.com/SimplifyJobs/Summer2027-Internships",
        url=(
            "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships"
            "/dev/.github/scripts/listings.json"
        ),
        filename="simplify_listings.json",
    ),
    SeedSource(
        key="ouckah",
        label="Vansh & Ouckah / CSCareers Summer 2027",
        repo="https://github.com/vanshb03/Summer2027-Internships",
        url=(
            "https://raw.githubusercontent.com/vanshb03/Summer2027-Internships"
            "/dev/.github/scripts/listings.json"
        ),
        filename="ouckah_listings.json",
    ),
)


def download(source: SeedSource, *, force: bool, timeout: float = 120.0) -> Path:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    target = RAW_DIR / source.filename
    if target.exists() and not force:
        print(f"[cached] {source.key}: {target.relative_to(REPO_ROOT)}")
        return target

    print(f"[fetch ] {source.key}: {source.url}")
    request = urllib.request.Request(source.url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = response.read()
    # Fail loudly rather than caching a 404 page.
    parsed = json.loads(payload)
    if not isinstance(parsed, list) or not parsed:
        raise SystemExit(f"{source.key}: expected a non-empty JSON list, got {type(parsed)}")
    target.write_bytes(payload)
    print(f"[ok    ] {source.key}: {len(parsed)} listings -> {target.relative_to(REPO_ROOT)}")
    return target


def load_cached() -> dict[str, list[dict[str, object]]]:
    """Load whatever has already been downloaded. Raises if anything is missing."""
    out: dict[str, list[dict[str, object]]] = {}
    for source in SEED_SOURCES:
        path = RAW_DIR / source.filename
        if not path.exists():
            raise FileNotFoundError(
                f"{path} is missing - run `python scripts/fetch_seed_listings.py` first"
            )
        out[source.key] = json.loads(path.read_text(encoding="utf-8"))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="re-download even if cached")
    args = parser.parse_args(argv)

    total = 0
    for source in SEED_SOURCES:
        path = download(source, force=args.force)
        total += len(json.loads(path.read_text(encoding="utf-8")))
    print(f"\n{total} raw listings available across {len(SEED_SOURCES)} seed sources.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
