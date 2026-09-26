"""Relevance filtering, deliberately separate from scraping (PRD §8)."""

from jobmonitor.filtering.relevance import (
    ADJACENT_SIGNALS,
    CORE_ROLE_SIGNALS,
    DEFAULT_MODEL,
    INTERNSHIP_SIGNALS,
    NEGATIVE_SIGNALS,
    Decision,
    JobFilter,
    Relevance,
    RelevanceModel,
    score_job,
)

__all__ = [
    "ADJACENT_SIGNALS",
    "CORE_ROLE_SIGNALS",
    "DEFAULT_MODEL",
    "INTERNSHIP_SIGNALS",
    "NEGATIVE_SIGNALS",
    "Decision",
    "JobFilter",
    "Relevance",
    "RelevanceModel",
    "score_job",
]
