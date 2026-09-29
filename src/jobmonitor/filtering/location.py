"""US-only location filtering.

Applied in the pipeline before relevance scoring, so a job outside the US is
never scored, stored or alerted on.

Careers sites describe location every way imaginable - "San Francisco, CA",
"US, WA, Seattle", "US-CA-Menlo Park", "Remote - USA", "Helsinki, fi",
"Bengaluru, Karnataka, IND", "SF, NY, SEA, Remote-US", "2 Locations" - so a
location is classified, not parsed:

* The text is split into places (``;``, ``|``, `` or ``, `` / ``).
* Each place is **US**, **non-US** or **unknown**, from the evidence it contains.
  *Strong* US evidence (a US country token, a full state name, a well-known US
  city, a ``US-``/``US,`` prefix) beats non-US evidence, so "London, New York"
  is US. A bare two-letter state abbreviation is *weak*: it only counts when
  nothing points abroad, so "Chennai, TN" (Tamil Nadu) is not Tennessee.
* The job is **US** if any place is (a multi-city posting that includes a US
  office is a US posting), **non-US** if some place is and none is US, and
  **unknown** otherwise ("Remote", "2 Locations", no location at all).

Unknown is kept by default - PRD §2: when uncertain, retain rather than silently
discard - and ``KEEP_UNKNOWN_LOCATIONS=false`` drops it too.

The word lists are built from the location strings every registry company
actually returned (tests/unit/test_location.py carries a sample of them).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from jobmonitor.models.job import Job


class Region(StrEnum):
    US = "us"
    NON_US = "non_us"
    UNKNOWN = "unknown"


US_STATES: dict[str, str] = {
    "AL": "alabama", "AK": "alaska", "AZ": "arizona", "AR": "arkansas",
    "CA": "california", "CO": "colorado", "CT": "connecticut", "DE": "delaware",
    "FL": "florida", "GA": "georgia", "HI": "hawaii", "ID": "idaho",
    "IL": "illinois", "IN": "indiana", "IA": "iowa", "KS": "kansas",
    "KY": "kentucky", "LA": "louisiana", "ME": "maine", "MD": "maryland",
    "MA": "massachusetts", "MI": "michigan", "MN": "minnesota", "MS": "mississippi",
    "MO": "missouri", "MT": "montana", "NE": "nebraska", "NV": "nevada",
    "NH": "new hampshire", "NJ": "new jersey", "NM": "new mexico", "NY": "new york",
    "NC": "north carolina", "ND": "north dakota", "OH": "ohio", "OK": "oklahoma",
    "OR": "oregon", "PA": "pennsylvania", "RI": "rhode island", "SC": "south carolina",
    "SD": "south dakota", "TN": "tennessee", "TX": "texas", "UT": "utah",
    "VT": "vermont", "VA": "virginia", "WA": "washington", "WV": "west virginia",
    "WI": "wisconsin", "WY": "wyoming", "DC": "district of columbia", "PR": "puerto rico",
}  # fmt: skip

US_COUNTRY_TOKENS = (
    "us", "usa", "u.s.", "u.s.a.", "united states", "united states of america", "america",
)  # fmt: skip

#: Unambiguous US places that often appear without a state.
US_CITIES = (
    "san francisco", "sf", "bay area", "silicon valley", "new york", "nyc", "new york city",
    "manhattan", "brooklyn", "seattle", "sea", "greater seattle area", "boston", "cambridge, ma",
    "austin", "chicago", "los angeles", "palo alto", "mountain view", "menlo park",
    "sunnyvale", "santa clara", "san jose", "san mateo", "foster city", "redwood city",
    "cupertino", "redmond", "bellevue", "kirkland", "washington, d.c.", "washington d.c.",
    "washington dc", "hawthorne", "starbase", "bastrop", "cape canaveral", "space coast",
    "costa mesa", "irvine", "san diego", "denver", "boulder", "atlanta", "miami", "dallas",
    "houston", "pittsburgh", "philadelphia", "detroit", "ann arbor", "salt lake city",
    "phoenix", "tempe", "raleigh", "durham", "portland", "minneapolis", "nashville",
    "arlington", "reston", "mclean", "herndon", "princeton", "stamford", "greenwich",
    "jersey city", "hoboken", "oakland", "berkeley", "emeryville", "santa monica",
    "culver city", "pasadena", "el segundo", "long beach", "madison", "columbus",
    "cincinnati", "cleveland", "st. louis", "kansas city", "baltimore", "richmond",
    "charlotte", "orlando", "tampa", "las vegas", "sacramento", "fremont", "milpitas",
    "livermore", "poway", "woodinville", "memphis", "huntsville", "tucson", "albuquerque",
    "waltham", "savannah", "wichita", "chandler", "billerica", "north reading",
)  # fmt: skip

#: Countries and territories, by name and by the ISO-3166 alpha-3 codes some
#: Workday tenants print ("Bengaluru, Karnataka, IND").
NON_US_COUNTRIES = (
    "canada", "can", "united kingdom", "uk", "u.k.", "gbr", "great britain", "england",
    "scotland", "wales", "northern ireland", "ireland", "irl", "india", "ind", "singapore",
    "sgp", "japan", "jpn", "china", "chn", "hong kong", "hkg", "taiwan", "twn", "korea",
    "south korea", "kor", "germany", "deu", "france", "fra", "netherlands", "nld",
    "belgium", "bel", "luxembourg", "lux", "switzerland", "che", "austria", "aut",
    "spain", "esp", "portugal", "prt", "italy", "ita", "poland", "pol", "czech republic",
    "czechia", "cze", "slovakia", "hungary", "hun", "romania", "rou", "bulgaria",
    "greece", "grc", "sweden", "swe", "norway", "nor", "denmark", "dnk", "finland", "fin",
    "estonia", "latvia", "lithuania", "ukraine", "ukr", "serbia", "croatia", "slovenia",
    "svn", "turkey", "türkiye", "tur", "israel", "isr", "united arab emirates", "uae",
    "are", "saudi arabia", "qatar", "egypt", "south africa", "zaf", "nigeria", "kenya",
    "australia", "aus", "new zealand", "nzl", "mexico", "mex", "brazil", "bra",
    "argentina", "arg", "chile", "chl", "colombia", "col", "peru", "costa rica",
    "philippines", "phl", "vietnam", "vnm", "thailand", "tha", "malaysia", "mys",
    "indonesia", "idn", "pakistan", "bangladesh", "sri lanka", "emea", "apac", "apj",
    "apjc", "latam", "europe", "asia", "svk", "morocco", "cyprus", "ethiopia", "armenia",
    "sau", "guatemala", "bolivia", "ecuador", "panama", "uruguay", "venezuela",
)  # fmt: skip

#: Provinces/regions that name no US place.
NON_US_REGIONS = (
    "british columbia", "ontario", "quebec", "québec", "alberta", "manitoba",
    "nova scotia", "karnataka", "telangana", "maharashtra", "tamil nadu", "haryana",
    "uttar pradesh", "new south wales", "victoria, aus", "queensland", "bavaria",
    "catalonia", "galicia", "silesian voivodeship", "masovian voivodeship", "west midlands",
    "greater london", "baden-wurttemberg", "baden-württemberg", "ile-de-france",
    "île-de-france", "bratislavský kraj", "trnavský kraj",
)  # fmt: skip

NON_US_CITIES = (
    "london", "toronto", "montreal", "montréal", "waterloo", "ottawa", "calgary",
    "bengaluru", "bangalore", "hyderabad", "pune", "mumbai", "chennai", "gurgaon",
    "gurugram", "noida", "new delhi", "delhi", "dublin", "paris", "berlin", "munich",
    "münchen", "frankfurt", "hamburg", "amsterdam", "zurich", "zürich", "geneva", "tokyo",
    "osaka", "seoul", "beijing", "shanghai", "shenzhen", "hangzhou", "taipei", "sydney",
    "melbourne", "tel aviv", "haifa", "warsaw", "krakow", "kraków", "wroclaw", "prague",
    "madrid", "barcelona", "lisbon", "milan", "rome", "stockholm", "copenhagen", "oslo",
    "helsinki", "vienna", "brussels", "edinburgh", "manchester", "cambridge, uk",
    "oxford", "reading", "belfast", "sao paulo", "são paulo", "mexico city", "bogota",
    "bogotá", "buenos aires", "santiago", "jakarta", "kuala lumpur", "bangkok", "manila",
    "ho chi minh city", "hanoi", "dubai", "abu dhabi", "riyadh", "cairo", "cape town",
    "johannesburg", "lagos", "nairobi", "auckland", "limassol", "ljubljana", "bucharest",
    "sofia", "athens", "istanbul", "kyiv", "tallinn", "vilnius", "riga", "gdansk",
    "cagliari", "vercelli", "castellbisbal", "sophia antipolis", "grenoble", "nantes",
    "montpellier", "bordeaux", "darlington", "causeway bay", "stuttgart", "bratislava",
    "belgrade", "lviv", "casablanca", "yerevan", "dammam", "jeddah", "ho chi minh",
    "gift city", "petah tikva", "herzliya", "gothenburg", "göteborg", "cdmx",
    "ciudad de méxico",
)  # fmt: skip

#: ISO-3166 alpha-2 codes that lead a Workday/Amazon-style place ("GB, London",
#: "IT, Milan") or trail an Eightfold one ("Helsinki, fi"). CA leads as Canada
#: ("CA-Toronto") but trails as California ("San Francisco, CA").
NON_US_ALPHA2 = (
    "ca", "gb", "uk", "ie", "in", "sg", "jp", "cn", "hk", "tw", "kr", "de", "fr", "nl",
    "be", "lu", "ch", "at", "es", "pt", "it", "pl", "cz", "sk", "hu", "ro", "bg", "gr",
    "se", "no", "dk", "fi", "ee", "lv", "lt", "ua", "rs", "hr", "si", "tr", "il", "ae",
    "sa", "qa", "eg", "za", "ng", "ke", "au", "nz", "mx", "br", "ar", "cl", "co", "pe",
    "cr", "ph", "vn", "th", "my", "id", "pk", "bd", "lk",
)  # fmt: skip

_PLACE_SPLIT = re.compile(r"\s*(?:;|\||\s+or\s+|\s/\s)\s*", re.IGNORECASE)


def _pattern(words: Iterable[str]) -> re.Pattern[str]:
    alternatives = sorted({re.escape(w) for w in words}, key=len, reverse=True)
    # Word-ish boundaries that also work around dots and hyphens ("U.S.", "US-CA").
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(alternatives) + r")(?![a-z0-9])")


_US_STRONG = _pattern([*US_COUNTRY_TOKENS, *US_STATES.values(), *US_CITIES, "remote-us"])
_NON_US = _pattern([*NON_US_COUNTRIES, *NON_US_REGIONS, *NON_US_CITIES])
_STATE_CODES = frozenset(code.lower() for code in US_STATES)
_ALPHA2 = frozenset(NON_US_ALPHA2)
_LEADING_CODE = re.compile(r"^([a-z]{2})\s*[-,]")
_TRAILING_CODE = re.compile(r"[,\s-]([a-z]{2})\s*$")
_ABBREVIATION = re.compile(r"(?:,|-|\()\s*([a-z]{2})(?![a-z0-9])")
#: Canadian provinces by code, trailing only: "Vancouver, BC".
_PROVINCE_CODE = re.compile(r",\s*(bc|on|qc|ab|mb|ns|nb|sk|nl|pe)\s*$")


class _Evidence(StrEnum):
    STRONG_US = "strong_us"
    NON_US = "non_us"
    WEAK_US = "weak_us"
    NONE = "none"


def _evidence(place: str) -> _Evidence:
    text = place.strip().lower()
    if not text:
        return _Evidence.NONE

    leading = _LEADING_CODE.match(text)
    if leading:
        code = leading.group(1)
        if code == "us":
            return _Evidence.STRONG_US
        if code in _ALPHA2 and code not in _STATE_CODES - {"ca"}:
            # A leading country code decides the place: "GB, London", "CA-Toronto".
            return _Evidence.NON_US

    if _US_STRONG.search(text):
        return _Evidence.STRONG_US
    if _NON_US.search(text) or _PROVINCE_CODE.search(text):
        return _Evidence.NON_US
    trailing = _TRAILING_CODE.search(text)
    if trailing and trailing.group(1) not in _STATE_CODES and trailing.group(1) in _ALPHA2:
        return _Evidence.NON_US  # "Helsinki, fi", "Causeway Bay, hk"

    # Weak: a state code after a comma, hyphen or bracket ("Hawthorne, CA",
    # "Remote - CA", "Billerica (MA)"), or standing alone ("MI").
    if text in _STATE_CODES or any(c in _STATE_CODES for c in _ABBREVIATION.findall(text)):
        return _Evidence.WEAK_US
    return _Evidence.NONE


_AS_REGION = {
    _Evidence.STRONG_US: Region.US,
    _Evidence.WEAK_US: Region.US,
    _Evidence.NON_US: Region.NON_US,
    _Evidence.NONE: Region.UNKNOWN,
}


def classify_place(place: str) -> Region:
    """One place, e.g. "Seattle, WA" or "GB, London"."""
    return _AS_REGION[_evidence(place)]


def classify_location(location: str | None) -> Region:
    """A whole location string, which may list several places.

    Strong US evidence anywhere wins; then any non-US evidence; a bare state
    code only decides when nothing else does ("Remote - Canada; Remote - CA").
    """
    if not location or not location.strip():
        return Region.UNKNOWN
    found = {_evidence(place) for place in _PLACE_SPLIT.split(location)}
    if _Evidence.STRONG_US in found:
        return Region.US
    if _Evidence.NON_US in found:
        return Region.NON_US
    if _Evidence.WEAK_US in found:
        return Region.US
    return Region.UNKNOWN


def is_allowed(job: Job, *, keep_unknown: bool = True) -> bool:
    region = classify_location(job.location)
    return region is Region.US or (region is Region.UNKNOWN and keep_unknown)


@dataclass(slots=True)
class LocationResult:
    kept: list[Job] = field(default_factory=list)
    outside_us: list[Job] = field(default_factory=list)
    unknown: int = 0


def split_by_location(
    jobs: Sequence[Job], *, us_only: bool = True, keep_unknown: bool = True
) -> LocationResult:
    """Partition ``jobs`` into those to keep and those outside the US."""
    result = LocationResult()
    for job in jobs:
        if not us_only:
            result.kept.append(job)
            continue
        region = classify_location(job.location)
        if region is Region.UNKNOWN:
            result.unknown += 1
        if region is Region.US or (region is Region.UNKNOWN and keep_unknown):
            result.kept.append(job)
        else:
            result.outside_us.append(job)
    return result


__all__: Sequence[str] = (
    "LocationResult",
    "Region",
    "classify_location",
    "classify_place",
    "is_allowed",
    "split_by_location",
)
