"""Relevance scoring and filtering (PRD §8).

Kept strictly separate from scraping: adapters report what a board says, this
module decides what the user cares about. That separation is what lets the
thresholds move without touching a single parser.

The model is a small, inspectable, *configurable* weighted-keyword scorer rather
than anything learned. Two reasons: every decision has to be explainable (the
score carries its reasons, which the health view and the tests both read), and a
personal project polling every 10 minutes should not be paying for inference.

Two thresholds, deliberately (PRD §2: *"prefer retaining a job with a lower
relevance score over silently discarding it"*):

* ``keep_threshold`` (default 35) - below this, drop entirely.
* ``notify_threshold`` (default 55) - below this, store but stay quiet.

So a borderline-but-plausible role lands in the feed without pinging the user's
phone, and nothing plausible is thrown away.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

from jobmonitor.config import FilterSettings
from jobmonitor.models.company import Company, Priority
from jobmonitor.models.job import Job

# --------------------------------------------------------------------- signals

#: Words that mean "this is an internship". Without one of these a posting is
#: capped below the keep threshold - the system monitors internships, and a
#: full-time senior role is not a near miss.
INTERNSHIP_SIGNALS: Final[tuple[str, ...]] = (
    "intern",
    "internship",
    "co-op",
    "coop",
    "co op",
    "summer analyst",
    "summer associate",
    "industrial placement",
    "student placement",
    "placement student",
    "working student",
    "apprentice",
    "apprenticeship",
    "trainee",
    "student",
    "early career program",
    "campus",
    "undergraduate",
    "new grad intern",
)

#: The primary target roles from PRD §2. A hit here is what makes a posting
#: clearly worth alerting about.
CORE_ROLE_SIGNALS: Final[tuple[str, ...]] = (
    "software engineer",
    "software developer",
    "software development",
    "software engineering",
    "swe",
    "backend",
    "back-end",
    "frontend",
    "front-end",
    "full stack",
    "full-stack",
    "fullstack",
    "infrastructure",
    "platform engineer",
    "platform engineering",
    "systems engineer",
    "systems engineering",
    "site reliability",
    "sre",
    "devops",
    "developer productivity",
    "developer tools",
    "devtools",
    "developer experience",
    "cloud engineer",
    "cloud engineering",
    "distributed systems",
    "compiler",
    "operating system",
    "kernel",
    "machine learning",
    "deep learning",
    "artificial intelligence",
    "ai engineer",
    "ai/ml",
    "mlops",
    "data engineer",
    "data engineering",
    "data platform",
    "security engineer",
    "security engineering",
    "application security",
    "product security",
    "offensive security",
    "cryptography",
    "quantitative developer",
    "quantitative engineer",
    "quantitative technologist",
    "quantitative research",
    "quant developer",
    "quant trading",
    "algorithmic trading",
    "trading systems",
    "firmware",
    "embedded software",
    "embedded systems",
    "robotics software",
    "robotics engineer",
    "autonomy",
    "perception",
    "controls engineer",
    "research engineer",
    "research scientist",
    "computer vision",
    "natural language processing",
    "nlp",
    "graphics engineer",
    "game engine",
    "network engineer",
    "database engineer",
    "storage engineer",
    "silicon design",
    "asic",
    "fpga",
    "verification engineer",
    "digital design",
    "chip design",
)

#: Adjacent/technical-but-vaguer signals. Worth keeping, not worth shouting about
#: on their own (PRD §2: "adjacent roles may be included when plausibly valuable").
ADJACENT_SIGNALS: Final[tuple[str, ...]] = (
    "engineer",
    "engineering",
    "developer",
    "programmer",
    "computer science",
    "technical",
    "technology",
    "data science",
    "data scientist",
    "data analyst",
    "analytics engineer",
    "qa engineer",
    "quality engineering",
    "test engineer",
    "automation engineer",
    "solutions engineer",
    "sales engineer",
    "support engineer",
    "technical program",
    "product manager",
    "hardware engineer",
    "electrical engineer",
    "systems analyst",
    "it",
    "information technology",
)

#: Categories PRD §2 says to avoid unless technical content makes them relevant.
NEGATIVE_SIGNALS: Final[tuple[str, ...]] = (
    "marketing",
    "brand",
    "advertising",
    "social media",
    "public relations",
    "communications intern",
    "content writer",
    "copywriter",
    "sales development",
    "account executive",
    "account manager",
    "business development",
    "human resources",
    "people operations",
    "talent acquisition",
    "recruiting",
    "recruiter",
    "accounting",
    "audit",
    "tax intern",
    "payroll",
    "legal intern",
    "paralegal",
    "compliance intern",
    "nursing",
    "nurse",
    "clinical assistant",
    "pharmacy",
    "civil engineer",
    "structural engineer",
    "construction",
    "interior design",
    "graphic design",
    "fashion",
    "culinary",
    "hospitality",
    "retail associate",
    "warehouse associate",
    "customer service",
    "teller",
    "real estate",
    "supply chain",
    "logistics coordinator",
    "procurement",
    "facilities",
    "environmental health",
    "safety intern",
)

#: Seniority words that contradict "internship" - a strong signal the posting was
#: matched by accident (e.g. a senior role whose text mentions an intern program).
SENIORITY_SIGNALS: Final[tuple[str, ...]] = (
    "senior",
    "staff engineer",
    "principal",
    "lead engineer",
    "manager,",
    "engineering manager",
    "director",
    "head of",
    "vice president",
    "vp of",
    "chief",
    "architect,",
    "distinguished",
    "fellow,",
)

#: Mechanical/chemical/etc. engineering is out of scope *unless* paired with
#: software content; handled as a conditional penalty rather than a hard exclude.
NON_SOFTWARE_ENGINEERING: Final[tuple[str, ...]] = (
    "mechanical engineer",
    "chemical engineer",
    "industrial engineer",
    "manufacturing engineer",
    "process engineer",
    "materials engineer",
    "biomedical engineer",
    "petroleum engineer",
    "mining engineer",
    "agricultural engineer",
)

_WORD_CHARS = re.compile(r"[a-z0-9+#./-]+")
#: Where a title stops naming the role and starts qualifying it:
#: "Data Science Intern - Influencer Marketing" -> role | team.
_TITLE_SPLIT_RE = re.compile(r"\s[-\u2013\u2014|:]\s|,\s|\s\(")
_PATTERN_CACHE: dict[str, re.Pattern[str]] = {}


def _normalize(text: str) -> str:
    """Lowercase and collapse to space-separated word tokens."""
    return f" {' '.join(_WORD_CHARS.findall(text.casefold()))} "


def _pattern(needle: str) -> re.Pattern[str]:
    """Word-boundary matcher, cached.

    Boundaries matter more than they look: a substring match would read
    "intern" out of "Internal Audit" and "international", inventing internships
    that do not exist.
    """
    compiled = _PATTERN_CACHE.get(needle)
    if compiled is None:
        compiled = re.compile(rf"(?<![a-z0-9]){re.escape(needle.casefold())}(?![a-z0-9])")
        _PATTERN_CACHE[needle] = compiled
    return compiled


def _matches(haystack: str, needles: Iterable[str]) -> tuple[str, ...]:
    """Every needle present in the normalized haystack, in declaration order."""
    return tuple(needle for needle in needles if _pattern(needle).search(haystack))


def split_role_and_qualifier(title: str) -> tuple[str, str]:
    """Split a title into the role it names and the team/qualifier that follows.

    Real titles routinely append an org that has nothing to do with the work:
    "Data Science Intern - Influencer Marketing AI Application" is a data science
    internship that happens to sit in an ads org. Penalising "marketing" there as
    hard as in "Marketing Intern" would discard a genuinely relevant role, which
    PRD §2 explicitly tells us not to do.
    """
    parts = _TITLE_SPLIT_RE.split(title, maxsplit=1)
    head = parts[0]
    qualifier = parts[1] if len(parts) > 1 else ""
    return head, qualifier


# ----------------------------------------------------------------------- model


@dataclass(frozen=True, slots=True)
class RelevanceModel:
    """Weights for the scorer. Every number here is meant to be tuned."""

    internship_base: int = 45
    core_role_bonus: int = 28
    #: Deliberately small: an adjacent role should be stored for the feed but
    #: should not, on its own, cross the notify threshold and buzz the phone.
    adjacent_role_bonus: int = 8
    #: Applied once if any core/adjacent signal appears in the description only.
    description_bonus: int = 7
    #: Per matching negative category, applied cumulatively up to the cap.
    negative_penalty: int = 32
    max_negative_penalty: int = 64
    #: A non-technical word in the team qualifier rather than the role name costs
    #: `negative_penalty // this`.
    qualifier_penalty_divisor: int = 4
    seniority_penalty: int = 40
    non_software_penalty: int = 22
    #: Score ceiling for a posting with no internship signal at all.
    non_internship_ceiling: int = 15
    priority_bonus: Mapping[str, int] = field(
        default_factory=lambda: {
            Priority.HIGH.value: 8,
            Priority.MEDIUM.value: 4,
            Priority.EXPERIMENTAL.value: 0,
        }
    )

    internship_signals: tuple[str, ...] = INTERNSHIP_SIGNALS
    core_role_signals: tuple[str, ...] = CORE_ROLE_SIGNALS
    adjacent_signals: tuple[str, ...] = ADJACENT_SIGNALS
    negative_signals: tuple[str, ...] = NEGATIVE_SIGNALS
    seniority_signals: tuple[str, ...] = SENIORITY_SIGNALS
    non_software_signals: tuple[str, ...] = NON_SOFTWARE_ENGINEERING


DEFAULT_MODEL: Final[RelevanceModel] = RelevanceModel()


@dataclass(frozen=True, slots=True)
class Relevance:
    """A score plus *why*, so every decision can be explained and asserted."""

    score: int
    is_internship: bool
    reasons: tuple[str, ...] = ()
    matched_core: tuple[str, ...] = ()
    matched_adjacent: tuple[str, ...] = ()
    matched_negative: tuple[str, ...] = ()

    @property
    def explanation(self) -> str:
        return "; ".join(self.reasons) if self.reasons else "no signals matched"


def score_job(
    job: Job, company: Company | None = None, *, model: RelevanceModel = DEFAULT_MODEL
) -> Relevance:
    """Score a posting 0-100 for relevance to a strong CS/SWE candidate."""
    title = _normalize(job.title)
    description = _normalize(job.description or "")
    employment = _normalize(job.employment_type or "")
    # Title is authoritative; employment_type often carries "Intern" when the
    # title does not (and vice versa), so both count as "the posting itself".
    headline = f"{title}{employment}"

    reasons: list[str] = []

    core_hits = _matches(headline, model.core_role_signals)
    adjacent_hits = _matches(headline, model.adjacent_signals)
    role_head, role_qualifier = split_role_and_qualifier(job.title)
    head_text = f"{_normalize(role_head)}{employment}"
    qualifier_text = _normalize(role_qualifier)
    negative_hits = _matches(headline, model.negative_signals)
    head_negatives = _matches(head_text, model.negative_signals)
    seniority_hits = _matches(headline, model.seniority_signals)
    non_software_hits = _matches(headline, model.non_software_signals)

    internship_hits = _matches(headline, model.internship_signals)
    is_internship = bool(internship_hits)
    if is_internship:
        reasons.append(f"internship signal: {internship_hits[0]}")
    elif seniority_hits:
        # Hard veto. A senior/staff/director title is not an internship whatever
        # its body text says - and senior job descriptions routinely mention
        # mentoring the intern cohort, which would otherwise match below.
        reasons.append(f"seniority in title vetoes internship: {seniority_hits[0]}")
    else:
        # Some boards only say "internship" in the body. Accept that; the title
        # still has to look like a role, which the scoring below enforces.
        internship_hits = _matches(description, model.internship_signals)
        is_internship = bool(internship_hits)
        if is_internship:
            reasons.append(f"internship signal in description only: {internship_hits[0]}")

    score = model.internship_base if is_internship else 0

    if core_hits:
        score += model.core_role_bonus
        reasons.append(f"core technical role: {', '.join(core_hits[:3])}")
    elif adjacent_hits:
        score += model.adjacent_role_bonus
        reasons.append(f"adjacent technical role: {', '.join(adjacent_hits[:3])}")
    else:
        description_core = _matches(description, model.core_role_signals)
        if description_core:
            score += model.description_bonus
            reasons.append(f"technical content in description: {description_core[0]}")

    if head_negatives:
        penalty = min(len(head_negatives) * model.negative_penalty, model.max_negative_penalty)
        score -= penalty
        reasons.append(f"non-technical role: {', '.join(head_negatives[:3])} (-{penalty})")
    elif negative_hits:
        # Only the team/qualifier is non-technical; the role itself is not.
        penalty = model.negative_penalty // model.qualifier_penalty_divisor
        score -= penalty
        reasons.append(f"non-technical team qualifier: {', '.join(negative_hits[:3])} (-{penalty})")
    _ = qualifier_text

    if non_software_hits and not core_hits:
        score -= model.non_software_penalty
        reasons.append(f"non-software engineering discipline: {non_software_hits[0]}")

    if seniority_hits:
        score -= model.seniority_penalty
        reasons.append(f"seniority conflicts with internship: {seniority_hits[0]}")

    if company is not None:
        bonus = model.priority_bonus.get(company.priority.value, 0)
        if bonus:
            score += bonus
            reasons.append(f"{company.priority.value}-priority company (+{bonus})")

    if not is_internship:
        capped = min(score, model.non_internship_ceiling)
        if capped != score:
            reasons.append(f"no internship signal: capped at {model.non_internship_ceiling}")
        score = capped

    score = max(0, min(100, score))
    return Relevance(
        score=score,
        is_internship=is_internship,
        reasons=tuple(reasons),
        matched_core=core_hits,
        matched_adjacent=adjacent_hits,
        matched_negative=negative_hits,
    )


# ---------------------------------------------------------------------- filter


@dataclass(frozen=True, slots=True)
class Decision:
    """What the pipeline should do with one posting."""

    job: Job
    relevance: Relevance
    keep: bool
    notify: bool
    immediate: bool

    @property
    def score(self) -> int:
        return self.relevance.score

    @property
    def reason(self) -> str:
        return self.relevance.explanation


class JobFilter:
    """Applies the configured thresholds to scored postings."""

    def __init__(
        self,
        settings: FilterSettings | None = None,
        *,
        model: RelevanceModel = DEFAULT_MODEL,
    ) -> None:
        self.settings = settings or FilterSettings()
        self.model = model

    def evaluate(self, job: Job, company: Company | None = None) -> Decision:
        relevance = score_job(job, company, model=self.model)
        keep = relevance.score >= self.settings.keep_threshold
        notify = keep and relevance.score >= self.settings.notify_threshold
        immediate = bool(
            notify
            and company is not None
            and company.priority.value in self.settings.immediate_alert_priorities
        )
        return Decision(job=job, relevance=relevance, keep=keep, notify=notify, immediate=immediate)

    def evaluate_all(self, jobs: Iterable[Job], company: Company | None = None) -> list[Decision]:
        return [self.evaluate(job, company) for job in jobs]

    def relevant(self, jobs: Iterable[Job], company: Company | None = None) -> list[Decision]:
        """Only the decisions worth persisting."""
        return [decision for decision in self.evaluate_all(jobs, company) if decision.keep]


__all__: Sequence[str] = (
    "ADJACENT_SIGNALS",
    "CORE_ROLE_SIGNALS",
    "DEFAULT_MODEL",
    "INTERNSHIP_SIGNALS",
    "NEGATIVE_SIGNALS",
    "NON_SOFTWARE_ENGINEERING",
    "SENIORITY_SIGNALS",
    "Decision",
    "JobFilter",
    "Relevance",
    "RelevanceModel",
    "score_job",
    "split_role_and_qualifier",
)
