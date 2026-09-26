"""Job identity: canonical URLs, stable job IDs, and content hashes.

Three distinct questions, three distinct functions:

* :func:`canonical_url` - "is this the same posting page?" Tracking parameters
  and case differences must not make one posting look like two.
* :func:`job_id` - "have we seen this posting before?" Prefers the provider's own
  identifier (``company + external_id``), and falls back to a hash of the
  normalized title/location/URL when a source gives us nothing stable (PRD §9).
* :func:`content_hash` - "has this posting changed?" Covers the fields a user
  would notice changing, and deliberately excludes our own bookkeeping.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from jobmonitor.models.job import Job

#: Query parameters that identify a *referrer*, never a posting.
TRACKING_PARAMS: frozenset[str] = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "utm_id",
        "gh_src",
        "gh_jid_src",
        "lever-source",
        "lever-origin",
        "lever-via",
        "ashby_jid_src",
        "source",
        "src",
        "ref",
        "referrer",
        "referral",
        "trackingid",
        "trk",
        "gclid",
        "fbclid",
        "mc_cid",
        "mc_eid",
        "recruiter",
        "iis",
        "iisn",
        "codes",
    }
)

_DEFAULT_PORTS: Mapping[str, str] = {"http": "80", "https": "443"}

_SEPARATOR_RE = re.compile(r"[\s‐-―_/,|]+")
_NOISE_RE = re.compile(r"[^\w\s]+")
_MULTISPACE_RE = re.compile(r"\s+")

#: Title decorations that carry no identity, e.g. "(Remote)", "- Summer 2027".
_TITLE_NOISE_WORDS: frozenset[str] = frozenset(
    {
        "summer",
        "fall",
        "spring",
        "winter",
        "remote",
        "hybrid",
        "onsite",
        "us",
        "usa",
        "canada",
    }
)

#: Location abbreviations seen in the wild, normalized so "SF" and
#: "San Francisco, CA" do not produce two different fingerprints.
_LOCATION_ALIASES: Mapping[str, str] = {
    "sf": "san francisco",
    "sfo": "san francisco",
    "nyc": "new york",
    "ny": "new york",
    "bay area": "san francisco",
    "sf bay area": "san francisco",
    "la": "los angeles",
    "dc": "washington",
    "washington dc": "washington",
    "seattle wa": "seattle",
    "remote us": "remote",
    "remote usa": "remote",
    "united states": "remote",
}


def canonical_url(url: str) -> str:
    """Normalize a posting URL for comparison (identity only, never for display).

    Lowercases scheme/host, drops the fragment, removes default ports and
    tracking parameters, and sorts the parameters that remain so that ordering
    differences do not create phantom new jobs.
    """
    if not url:
        return ""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = parts.hostname or ""
    port = parts.port
    netloc = host
    if port is not None and str(port) != _DEFAULT_PORTS.get(scheme, ""):
        netloc = f"{host}:{port}"

    path = parts.path or "/"
    if len(path) > 1:
        path = path.rstrip("/")

    kept = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=False)
        if key.lower() not in TRACKING_PARAMS
    ]
    query = urlencode(sorted(kept))
    return urlunsplit((scheme, netloc, path, query, ""))


def normalize_title(title: str) -> str:
    """Reduce a job title to its identity-bearing words.

    >>> normalize_title("Software Engineer Intern (Summer 2027) - Remote")
    'engineer intern software'
    """
    text = title.casefold()
    text = re.sub(r"\([^)]*\)", " ", text)
    text = _SEPARATOR_RE.sub(" ", text)
    text = _NOISE_RE.sub(" ", text)
    words = [
        word
        for word in _MULTISPACE_RE.sub(" ", text).split()
        if word and not word.isdigit() and word not in _TITLE_NOISE_WORDS
    ]
    # Sorted: some ATSes reorder title components between responses.
    return " ".join(sorted(set(words)))


def normalize_location(location: str | None) -> str:
    """Canonical location string, tolerant of the many ways sources spell one."""
    if not location:
        return ""
    text = location.casefold()
    text = _SEPARATOR_RE.sub(" ", text)
    text = _NOISE_RE.sub(" ", text)
    text = _MULTISPACE_RE.sub(" ", text).strip()
    return _LOCATION_ALIASES.get(text, text)


def _digest(parts: Iterable[str]) -> str:
    joined = "\x1f".join(parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def company_slug(company: str) -> str:
    """Filesystem/key-safe company token used inside composite IDs."""
    return re.sub(r"[^a-z0-9]+", "-", company.casefold()).strip("-")


def job_id(job: Job) -> str:
    """Stable identity for a posting.

    Preferred form is ``<company-slug>:<external-id>`` because provider IDs
    survive title edits and URL churn. When a source exposes no usable ID, falls
    back to a hash over company + normalized title + normalized location +
    canonical URL (PRD §9).
    """
    slug = company_slug(job.company)
    if job.external_id:
        return f"{slug}:{job.external_id}"
    fingerprint = _digest(
        (
            slug,
            normalize_title(job.title),
            normalize_location(job.location),
            canonical_url(job.url),
        )
    )
    return f"{slug}:h:{fingerprint[:32]}"


def content_hash(job: Job) -> str:
    """Hash of the user-visible content, used to detect *updated* postings.

    ``date_posted`` is deliberately **excluded**. PRD §9 already notes that
    careers sites give unreliable posting timestamps, and some are worse than
    unreliable: Workday reports relative text ("Posted 5 Days Ago"), so the
    absolute date we derive from it moves every single day even when the posting
    has not changed at all. Including it would mark every Workday job as
    "updated" on every poll. ``first_seen``/``last_seen`` carry the timing
    information that actually matters.
    """
    return _digest(
        (
            job.title.casefold(),
            (job.location or "").casefold(),
            canonical_url(job.url),
            (job.employment_type or "").casefold(),
            (job.description or "").casefold(),
        )
    )


def deduplicate(jobs: Iterable[Job]) -> list[Job]:
    """Collapse duplicate postings *within one source response*.

    Sources genuinely repeat postings - the same role listed under several
    locations, or a board paginating with overlap. First occurrence wins so the
    ordering the source chose is preserved.
    """
    seen: set[str] = set()
    out: list[Job] = []
    for job in jobs:
        identity = job_id(job)
        if identity in seen:
            continue
        seen.add(identity)
        out.append(job)
    return out


__all__: Sequence[str] = (
    "TRACKING_PARAMS",
    "canonical_url",
    "company_slug",
    "content_hash",
    "deduplicate",
    "job_id",
    "normalize_location",
    "normalize_title",
)
