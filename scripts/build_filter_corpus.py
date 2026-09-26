#!/usr/bin/env python3
"""Regenerate the real-world filter regression corpus.

Samples real internship titles from the seed repositories and records the verdict
the current filter gives each one. ``tests/unit/test_filter_corpus.py`` replays
the result, so any change to the relevance model that moves a *real* title across
the keep/notify boundary surfaces as a reviewable diff instead of a silent
behaviour change.

    python scripts/build_filter_corpus.py            # show the diff, write nothing
    python scripts/build_filter_corpus.py --write    # accept the new verdicts

Review the diff before accepting it: that review is the whole point.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from fetch_seed_listings import load_cached  # noqa: E402
from jobmonitor.errors import InvalidJobError  # noqa: E402
from jobmonitor.filtering import JobFilter  # noqa: E402
from jobmonitor.models.job import Job  # noqa: E402

CORPUS_PATH = REPO_ROOT / "tests" / "fixtures" / "filter_corpus.json"
SAMPLE_SIZE = 200
SAMPLE_SEED = 42

COMMENT = [
    "200 real internship titles sampled (seed=42) from the distinct titles in the",
    "two mandated seed repositories, with the verdict the shipped filter gives each.",
    "Committed as a REGRESSION CORPUS: tests/unit/test_filter_corpus.py replays it,",
    "so any change to the scoring model that moves a real-world title across the",
    "keep/notify boundary shows up as a diff to review rather than as a silent",
    "behaviour change. Regenerate with scripts/build_filter_corpus.py --write.",
    "The verdicts were reviewed by hand; see TESTING.md.",
]


def verdict_for(title: str, job_filter: JobFilter) -> dict[str, object] | None:
    try:
        job = Job(company="X", title=title, url="https://x.example.com/j/1", source="greenhouse")
    except InvalidJobError:
        return None
    decision = job_filter.evaluate(job)
    return {
        "title": title,
        "score": decision.score,
        "verdict": "notify" if decision.notify else ("keep" if decision.keep else "drop"),
    }


def build() -> dict[str, object]:
    rows = [row for listings in load_cached().values() for row in listings]
    titles = sorted({str(row.get("title") or "").strip() for row in rows if row.get("title")})
    sample = random.Random(SAMPLE_SEED).sample(titles, min(SAMPLE_SIZE, len(titles)))
    job_filter = JobFilter()
    cases = [case for title in sample if (case := verdict_for(title, job_filter))]
    return {
        "_comment": COMMENT,
        "sample_seed": SAMPLE_SEED,
        "distinct_titles_available": len(titles),
        "cases": cases,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="accept the new verdicts")
    args = parser.parse_args(argv)

    fresh = build()
    payload = json.dumps(fresh, indent=1, ensure_ascii=False) + "\n"

    counts: dict[str, int] = {}
    for case in fresh["cases"]:  # type: ignore[union-attr]
        verdict = str(case["verdict"])  # type: ignore[index]
        counts[verdict] = counts.get(verdict, 0) + 1
    print(f"{len(fresh['cases'])} cases: {counts}")  # type: ignore[arg-type]

    if CORPUS_PATH.exists():
        existing = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
        old = {c["title"]: c for c in existing.get("cases", [])}
        changes = [
            (case["title"], old[case["title"]]["verdict"], case["verdict"])
            for case in fresh["cases"]  # type: ignore[union-attr]
            if case["title"] in old and old[case["title"]]["verdict"] != case["verdict"]  # type: ignore[index]
        ]
        if changes:
            print(f"\n{len(changes)} verdict change(s) - review each before accepting:")
            for title, before, after in changes:
                print(f"  {before:>6} -> {after:<6}  {title[:90]}")
        else:
            print("\nno verdict changes")

    if args.write:
        CORPUS_PATH.write_text(payload, encoding="utf-8")
        print(f"\nwrote {CORPUS_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
