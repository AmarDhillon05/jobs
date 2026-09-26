"""Level 1 - the filter replayed against 200 REAL internship titles.

Hand-written test cases prove the filter does what I expected. This proves it
does something sensible on titles nobody designed for it: a sample of real
postings from the two mandated seed repositories, with the verdict each one gets
recorded in ``tests/fixtures/filter_corpus.json``.

Its job is to make model changes *visible*. Tune a weight and this test names
every real-world title that crossed a boundary, which is the moment to decide
whether the tuning was an improvement. Regenerate deliberately with
``python scripts/build_filter_corpus.py --write``.
"""

from __future__ import annotations

import pytest
from tests.support.http import load_fixture

from jobmonitor.filtering import JobFilter
from jobmonitor.models.job import Job


@pytest.fixture(scope="module")
def corpus() -> dict:
    return load_fixture("filter_corpus.json")


@pytest.fixture(scope="module")
def replayed(corpus: dict) -> list[tuple[dict, int, str]]:
    job_filter = JobFilter()
    out = []
    for case in corpus["cases"]:
        job = Job(
            company="X", title=case["title"], url="https://x.example.com/j/1", source="greenhouse"
        )
        decision = job_filter.evaluate(job)
        verdict = "notify" if decision.notify else ("keep" if decision.keep else "drop")
        out.append((case, decision.score, verdict))
    return out


def test_corpus_is_a_meaningful_sample(corpus: dict) -> None:
    assert len(corpus["cases"]) >= 190
    assert corpus["distinct_titles_available"] > 5000
    verdicts = {case["verdict"] for case in corpus["cases"]}
    assert verdicts == {"notify", "keep", "drop"}


def test_no_real_world_title_changed_verdict(replayed: list[tuple[dict, int, str]]) -> None:
    drifted = [
        f"{case['verdict']} -> {verdict}: {case['title']}"
        for case, _score, verdict in replayed
        if verdict != case["verdict"]
    ]
    assert not drifted, (
        f"{len(drifted)} real title(s) changed verdict. If the change is an "
        "improvement, accept it with `python scripts/build_filter_corpus.py --write`:\n"
        + "\n".join(f"  {line}" for line in drifted[:25])
    )


def test_scores_are_unchanged(replayed: list[tuple[dict, int, str]]) -> None:
    drifted = [
        f"{case['title']}: {case['score']} -> {score}"
        for case, score, _verdict in replayed
        if score != case["score"]
    ]
    assert not drifted, "\n".join(drifted[:25])


def test_every_notified_title_mentions_an_internship(
    replayed: list[tuple[dict, int, str]],
) -> None:
    """Nothing full-time should ever reach the user's phone."""
    from jobmonitor.filtering.relevance import INTERNSHIP_SIGNALS

    for case, _score, verdict in replayed:
        if verdict != "notify":
            continue
        lowered = case["title"].casefold()
        assert any(signal in lowered for signal in INTERNSHIP_SIGNALS), case["title"]


def test_the_sample_is_dominated_by_retained_roles(
    replayed: list[tuple[dict, int, str]],
) -> None:
    """PRD §2's tie-breaker, measured: retention should dominate discarding."""
    kept = sum(1 for _case, _score, verdict in replayed if verdict != "drop")
    assert kept / len(replayed) > 0.8
