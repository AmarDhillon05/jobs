"""The US-only location filter (jobmonitor.filtering.location).

Every string below was returned by a registry company's live board (2026-09-29
sample of 26,438 postings / 4,537 distinct locations), so these are the shapes
the filter actually meets, not invented ones.
"""

from __future__ import annotations

import pytest

from jobmonitor.filtering.location import (
    Region,
    classify_location,
    classify_place,
    is_allowed,
    split_by_location,
)
from jobmonitor.models.job import Job

pytestmark = pytest.mark.unit

US = [
    "Costa Mesa, California, United States; Costa Mesa, CA (OC-00)",
    "Seattle, Washington, USA",
    "San Francisco; San Francisco, California",
    "Hawthorne, CA",
    "Bastrop, TX",
    "US, CA, Santa Clara",
    "US, WA, Seattle",
    "US-CA-Menlo Park; Menlo Park, California",
    "Redmond, WA",
    "Greater Seattle Area",
    "Washington, D.C.",
    "Remote - USA; US - Remote Zone 1 (Job Requisitions Only)",
    "Remote - US",
    "Remote - United States",
    "United States",
    "Office - USA - CA - Headquarters",
    "Space Coast, FL",
    "San Francisco, CA | New York City, NY; San Francisco, CA",
    "Mountain View, California, us",
    "Pleasanton, CALIFORNIA, us",
    "SF, NY, SEA, Remote-US; US",
    "NYC, SF, Seattle, US; US",
    "San Francisco Or New York; US",
    "Mountain View, CA, US; Mountain View (US-MTV-EMF680)",
    "Maryland, USA, Remote; Virginia, USA, Remote; Remote - DC",
    "McGregor, TX",
    "Honolulu, HI",
    "Billerica (MA)",
    "Remote - GA; Remote - CA",
    "MI",
    "Vancouver, WA - 805 Broadway",
    "New York",
    "Austin",
    "Chicago",
]

#: Multi-location postings that include a US office are US postings.
US_AMONG_OTHERS = [
    "New York, NY; Toronto, Canada",
    "Paris; London; New York; Toronto; Montreal",
    "London, New York; London; New York City",
    "Hybrid; Austin, TX; London, United Kingdom",
    "San Francisco, CA • New York, NY • United States; Canada; US",
    "Remote - Canada; Remote - SF Bay Area",
    "US-San Francisco; US-New York City; US-Remote; CA-Toronto; CA-Remote; US",
    "São Paulo, São Paulo, Brazil; New York, NY; Salt Lake City, UT",
    "Bay Area, CA, United States of America; AU - ACT - Remote",
]

NON_US = [
    "Bengaluru, Karnataka, IND",
    "Singapore",
    "London",
    "London, United Kingdom",
    "Vancouver, British Columbia, CAN",
    "Tokyo, Japan",
    "Hyderabad, Telangana, IND",
    "Ireland - Dublin",
    "Hong Kong, Hong Kong; Hong Kong",
    "GB, London",
    "IT, Milan",
    "ES, B, Barcelona",
    "CA-Toronto",
    "Toronto, ON, CA; Toronto",
    "Hybrid; Vancouver, BC",
    "Helsinki, fi",
    "Causeway Bay, hk",
    "Hyderabad, in",
    "Munich, de",
    "Montreal, QUEBEC, ca",
    "Petah Tikva, il",
    "Paris; Paris, Ile-de-France",
    "Remote - Canada; Remote - CA",  # "CA" is weak; Canada decides
    "In-Office; Remote India",  # "In-Office" is not Indiana
    "Chennai, TN",  # Tamil Nadu, not Tennessee
    "Remote - UK; UK - Remote Zone 1 (Job Requisitions Only)",
    "N/A; India Locations",
    "Hybrid; Chile",
    "VNM",
    "Sydney, New South Wales, AUS",
    "KR-Seoul; Seoul",
    "Stuttgart; Stuttgart, Baden-Wurttemberg",
]

#: Nothing either way: kept by default (PRD §2, retain when uncertain).
UNKNOWN = ["2 Locations", "Remote", "remote", "Virtual", "Remote - Americas - Remote", "", None]


@pytest.mark.parametrize("location", US + US_AMONG_OTHERS)
def test_us_locations(location: str) -> None:
    assert classify_location(location) is Region.US


@pytest.mark.parametrize("location", NON_US)
def test_non_us_locations(location: str) -> None:
    assert classify_location(location) is Region.NON_US


@pytest.mark.parametrize("location", UNKNOWN)
def test_unknown_locations(location: str | None) -> None:
    assert classify_location(location) is Region.UNKNOWN


def test_a_single_place() -> None:
    assert classify_place("Seattle, WA") is Region.US
    assert classify_place("GB, London") is Region.NON_US
    assert classify_place("Remote") is Region.UNKNOWN


def job(location: str | None, identifier: str = "1") -> Job:
    return Job(
        company="TestCo",
        title="Software Engineer Intern",
        url=f"https://jobs.example.test/{identifier}",
        source="greenhouse",
        external_id=identifier,
        location=location,
    )


class TestSplitting:
    def test_keeps_us_and_unknown_and_drops_the_rest(self) -> None:
        jobs = [job("Seattle, WA", "1"), job("London, UK", "2"), job("Remote", "3"), job(None, "4")]
        result = split_by_location(jobs)
        assert [j.external_id for j in result.kept] == ["1", "3", "4"]
        assert [j.external_id for j in result.outside_us] == ["2"]
        assert result.unknown == 2

    def test_unknown_can_be_dropped_too(self) -> None:
        jobs = [job("Seattle, WA", "1"), job("Remote", "2")]
        result = split_by_location(jobs, keep_unknown=False)
        assert [j.external_id for j in result.kept] == ["1"]
        assert [j.external_id for j in result.outside_us] == ["2"]

    def test_switched_off_keeps_everything(self) -> None:
        jobs = [job("London, UK", "1"), job("Tokyo, Japan", "2")]
        assert len(split_by_location(jobs, us_only=False).kept) == 2

    def test_is_allowed(self) -> None:
        assert is_allowed(job("Austin, TX"))
        assert not is_allowed(job("Berlin, Germany"))
        assert is_allowed(job("Remote"))
        assert not is_allowed(job("Remote"), keep_unknown=False)
