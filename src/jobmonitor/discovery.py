"""Company discovery helpers.

Build-time logic for turning the two mandated community repositories (PRD §4)
into monitoring targets:

* :func:`normalize_company_name` collapses aliases so "Stripe, Inc." and
  "stripe" become one company.
* :func:`detect_provider` reads an application URL and recovers the ATS provider
  and the exact configuration our adapters need.

Both are pure functions over strings, so the whole discovery step is unit
testable with no network access.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

# Suffixes and decorations that do not distinguish one employer from another.
_LEGAL_SUFFIXES: tuple[str, ...] = (
    "incorporated",
    "inc",
    "llc",
    "l l c",
    "ltd",
    "limited",
    "corporation",
    "corp",
    "co",
    "plc",
    "gmbh",
    "sa",
    "nv",
    "ag",
    "holdings",
    "group",
    "technologies",
    "technology",
    "labs",
    "laboratories",
    "systems",
    "software",
    "company",
    "the",
)

_PAREN_RE = re.compile(r"\([^)]*\)")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
# Emoji / legend markers used by the seed repositories' markdown tables.
_MARKER_RE = re.compile(r"[\U0001f000-\U0001faff☀-➿️⬀-⯿]")


def normalize_company_name(name: str) -> str:
    """Return a canonical key for alias de-duplication.

    >>> normalize_company_name("Stripe, Inc.")
    'stripe'
    >>> normalize_company_name("Hudson River Trading LLC")
    'hudsonriver'
    >>> normalize_company_name("D. E. Shaw")
    'deshaw'
    """
    text = _MARKER_RE.sub(" ", name)
    text = _PAREN_RE.sub(" ", text)
    text = text.casefold()
    # Drop a trailing "- something" qualifier, e.g. "Acme - University Program".
    text = re.split(r"\s[-–—]\s", text)[0]
    words = [w for w in _NON_ALNUM_RE.sub(" ", text).split() if w]
    while words and words[-1] in _LEGAL_SUFFIXES:
        words.pop()
    while words and words[0] in _LEGAL_SUFFIXES:
        words.pop(0)
    return "".join(words)


# --------------------------------------------------------------------- providers

#: ATS providers with a reusable adapter in ``jobmonitor.scrapers``.
SUPPORTED_PROVIDERS: frozenset[str] = frozenset(
    {
        "greenhouse",
        "lever",
        "ashby",
        "workday",
        "smartrecruiters",
        "workable",
        "rippling",
    }
)

#: Recognised but not adapted. Kept so the coverage report can explain *why* a
#: company is unsupported instead of lumping it in with "unknown".
RECOGNISED_UNSUPPORTED: Mapping[str, str] = {
    "icims": "iCIMS - no public JSON listing endpoint; server-rendered + bot-protected",
    "oracle": "Oracle Fusion/Taleo - per-tenant REST shape, heavy anti-automation",
    "jobvite": "Jobvite - listing feed varies per tenant",
    "eightfold": "Eightfold AI - POST search API behind bot protection",
    "avature": "Avature - per-tenant markup, no stable public feed",
    "jibeapply": "Jibe/Radancy - server-rendered, bot-protected",
    "workday-site": "Workday 'myworkdaysite' variant - tenant not recoverable from URL alone",
    "phenom": "Phenom People - GraphQL search behind bot protection",
    "custom": "Company-owned careers site with no discovered public feed",
}

_WORKDAY_HOST_RE = re.compile(
    r"^(?P<tenant>[a-z0-9][a-z0-9-]*)\.(?P<dc>wd\d+)\.myworkdayjobs\.com$"
)
_WORKDAY_SITE_RE = re.compile(r"^(?P<dc>wd\d+)\.myworkdaysite\.com$")
_LOCALE_RE = re.compile(r"^[a-z]{2}(?:-[A-Za-z]{2,4})?$")
# Greenhouse hosts that are shared infrastructure, not a board slug.
_GREENHOUSE_SHARED_HOSTS = frozenset({"job-boards", "boards", "boards-api", "my", "app", "www"})


@dataclass(frozen=True, slots=True)
class ProviderGuess:
    """A provider + the adapter configuration recovered from a posting URL."""

    provider: str
    config: dict[str, Any]
    #: Host the guess came from, for diagnostics.
    host: str = ""
    #: False for providers in :data:`RECOGNISED_UNSUPPORTED` / unknown hosts.
    supported: bool = True
    reason: str = ""

    @property
    def is_configured(self) -> bool:
        """True when the config is complete enough to actually fetch with."""
        return self.supported and bool(self.config)


def _path_segments(path: str) -> list[str]:
    return [segment for segment in path.split("/") if segment]


def _detect_greenhouse(host: str, segments: list[str]) -> ProviderGuess | None:
    subdomain = host.split(".")[0]
    if subdomain not in _GREENHOUSE_SHARED_HOSTS:
        # e.g. acme.greenhouse.io/acme -> board token is the subdomain
        return ProviderGuess("greenhouse", {"board_token": subdomain}, host)
    if segments and segments[0] not in {"embed", "jobs", "job_app", "v1"}:
        return ProviderGuess("greenhouse", {"board_token": segments[0]}, host)
    # boards.greenhouse.io/embed/job_app?token=123 hides the board token.
    return ProviderGuess(
        "greenhouse", {}, host, reason="board token not present in embed-style URL"
    )


def _detect_workday(host: str, segments: list[str]) -> ProviderGuess | None:
    match = _WORKDAY_HOST_RE.match(host)
    if match:
        tenant = match.group("tenant")
        # Path is [<locale>]/<site>/job/... - the site is the first non-locale part.
        site = next((s for s in segments if not _LOCALE_RE.match(s)), None)
        config: dict[str, Any] = {"tenant": tenant, "host": host}
        if site and site != "job":
            config["site"] = site
            return ProviderGuess("workday", config, host)
        return ProviderGuess("workday", {}, host, reason="career-site id not present in URL path")
    if _WORKDAY_SITE_RE.match(host):
        return ProviderGuess(
            "workday",
            {},
            host,
            supported=False,
            reason=RECOGNISED_UNSUPPORTED["workday-site"],
        )
    return None


def detect_provider(url: str) -> ProviderGuess:
    """Recover the ATS provider and adapter config from a job application URL."""
    if not url or not url.strip():
        return ProviderGuess("custom", {}, "", supported=False, reason="empty url")

    parsed = urlparse(url.strip())
    host = (parsed.netloc or "").lower()
    if host.endswith(":443") or host.endswith(":80"):
        host = host.rsplit(":", 1)[0]
    segments = _path_segments(parsed.path)

    if not host:
        return ProviderGuess("custom", {}, "", supported=False, reason="url has no host")

    if "greenhouse.io" in host:
        guess = _detect_greenhouse(host, segments)
        if guess:
            return guess

    # A Greenhouse board rendered on the company's own domain. Greenhouse's embed
    # script appends `gh_jid` (its job id), so the parameter is hard evidence of
    # the provider even though the *board token* never appears in the URL. Saying
    # "greenhouse, token unknown" is strictly more useful than "custom": it tells
    # the coverage report what to configure, and it is how Stripe, Waymo, Datadog
    # and Hudson River Trading were found.
    query = parse_qs(parsed.query)
    if "gh_jid" in query or "gh_src" in query:
        return ProviderGuess(
            "greenhouse",
            {},
            host,
            reason=(
                "greenhouse-backed board on a company-owned domain "
                "(gh_jid present); board token is not in the URL"
            ),
        )

    if "lever.co" in host:
        # jobs.lever.co/<site>/<posting-id>
        if segments:
            return ProviderGuess("lever", {"site": segments[0]}, host)
        return ProviderGuess("lever", {}, host, reason="no lever site in path")

    if "ashbyhq.com" in host:
        if segments:
            return ProviderGuess("ashby", {"job_board_name": segments[0]}, host)
        return ProviderGuess("ashby", {}, host, reason="no ashby board in path")

    if "smartrecruiters.com" in host:
        if segments:
            return ProviderGuess("smartrecruiters", {"company_id": segments[0]}, host)
        return ProviderGuess("smartrecruiters", {}, host, reason="no company id in path")

    workday = _detect_workday(host, segments)
    if workday:
        return workday

    if "workable.com" in host:
        interesting = [s for s in segments if s not in {"j", "view", "api"}]
        if interesting:
            return ProviderGuess("workable", {"subdomain": interesting[0]}, host)
        return ProviderGuess("workable", {}, host, reason="no workable account in path")

    if "rippling.com" in host or "rippling-ats.com" in host:
        interesting = [s for s in segments if not _LOCALE_RE.match(s)]
        if interesting:
            return ProviderGuess("rippling", {"board_slug": interesting[0]}, host)
        return ProviderGuess("rippling", {}, host, reason="no rippling board in path")

    for token, reason in RECOGNISED_UNSUPPORTED.items():
        if token in host:
            return ProviderGuess(token, {}, host, supported=False, reason=reason)

    if "oraclecloud.com" in host or "taleo.net" in host:
        return ProviderGuess(
            "oracle", {}, host, supported=False, reason=RECOGNISED_UNSUPPORTED["oracle"]
        )

    return ProviderGuess(
        "custom", {}, host, supported=False, reason=RECOGNISED_UNSUPPORTED["custom"]
    )


def careers_url_for(provider: str, config: Mapping[str, Any]) -> str | None:
    """Canonical human-facing careers URL for a provider configuration."""
    if provider == "greenhouse" and config.get("board_token"):
        return f"https://job-boards.greenhouse.io/{config['board_token']}"
    if provider == "lever" and config.get("site"):
        return f"https://jobs.lever.co/{config['site']}"
    if provider == "ashby" and config.get("job_board_name"):
        return f"https://jobs.ashbyhq.com/{config['job_board_name']}"
    if provider == "smartrecruiters" and config.get("company_id"):
        return f"https://jobs.smartrecruiters.com/{config['company_id']}"
    if provider == "workable" and config.get("subdomain"):
        return f"https://apply.workable.com/{config['subdomain']}/"
    if provider == "rippling" and config.get("board_slug"):
        return f"https://ats.rippling.com/{config['board_slug']}/jobs"
    if provider == "workday" and config.get("host") and config.get("site"):
        return f"https://{config['host']}/en-US/{config['site']}"
    return None


# ------------------------------------------------------------------ aggregation


@dataclass(frozen=True, slots=True)
class DiscoveredCompany:
    """One employer as observed across the seed repositories."""

    canonical_name: str
    normalized: str
    observed_names: tuple[str, ...]
    sources: tuple[str, ...]
    listing_count: int
    #: (provider, config-as-sorted-items) -> times observed, most common first.
    provider_counts: tuple[tuple[ProviderGuess, int], ...]

    @property
    def best_guess(self) -> ProviderGuess | None:
        """Most frequently observed *configured, supported* provider."""
        for guess, _count in self.provider_counts:
            if guess.is_configured:
                return guess
        return self.provider_counts[0][0] if self.provider_counts else None


def _guess_key(guess: ProviderGuess) -> tuple[Any, ...]:
    return (guess.provider, tuple(sorted(guess.config.items())))


def aggregate_listings(listings: Iterable[Mapping[str, Any]]) -> list[DiscoveredCompany]:
    """Collapse raw seed listings into one entry per employer.

    Accepts the ``listings.json`` shape published by both seed repositories:
    ``{"company_name": ..., "url": ..., "source": ...}``. Unknown keys are
    ignored, and rows missing a company or URL are skipped rather than fatal -
    the seed files are community maintained and routinely contain partial rows.
    """
    names: dict[str, Counter[str]] = {}
    sources: dict[str, set[str]] = {}
    counts: Counter[str] = Counter()
    providers: dict[str, dict[tuple[Any, ...], tuple[ProviderGuess, int]]] = {}

    for row in listings:
        raw_name = str(row.get("company_name") or "").strip()
        url = str(row.get("url") or "").strip()
        if not raw_name or not url:
            continue
        key = normalize_company_name(raw_name)
        if not key:
            continue

        names.setdefault(key, Counter())[raw_name] += 1
        sources.setdefault(key, set()).add(str(row.get("source") or "unknown"))
        counts[key] += 1

        guess = detect_provider(url)
        bucket = providers.setdefault(key, {})
        gkey = _guess_key(guess)
        existing = bucket.get(gkey)
        bucket[gkey] = (guess, (existing[1] if existing else 0) + 1)

    out: list[DiscoveredCompany] = []
    for key, name_counter in names.items():
        ranked = sorted(providers.get(key, {}).values(), key=lambda pair: -pair[1])
        out.append(
            DiscoveredCompany(
                canonical_name=name_counter.most_common(1)[0][0],
                normalized=key,
                observed_names=tuple(sorted(name_counter)),
                sources=tuple(sorted(sources[key])),
                listing_count=counts[key],
                provider_counts=tuple(ranked),
            )
        )
    out.sort(key=lambda company: (-company.listing_count, company.normalized))
    return out


__all__: Sequence[str] = (
    "RECOGNISED_UNSUPPORTED",
    "SUPPORTED_PROVIDERS",
    "DiscoveredCompany",
    "ProviderGuess",
    "aggregate_listings",
    "careers_url_for",
    "detect_provider",
    "normalize_company_name",
)
