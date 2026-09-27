"""The registry build (scripts/build_company_registry.py).

Only the rule added during live validation is pinned here: a company resolved
from seed URLs may carry editorial ``provider_config_overrides`` and notes. The
seed URLs still decide *where* the company posts; the override only tunes *how*
its board is read (RTX and Micron needed different Workday search terms).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from jobmonitor.discovery import DiscoveredCompany, ProviderGuess
from jobmonitor.models.company import SupportStatus

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from build_company_registry import resolve

pytestmark = pytest.mark.unit

RTX_SEEN = DiscoveredCompany(
    canonical_name="RTX",
    normalized="rtx",
    observed_names=("RTX",),
    sources=("simplify", "ouckah"),
    listing_count=344,
    provider_counts=(
        (
            ProviderGuess(
                "workday",
                {
                    "tenant": "globalhr",
                    "host": "globalhr.wd5.myworkdayjobs.com",
                    "site": "rec_rtx_ext_gateway",
                },
                "globalhr.wd5.myworkdayjobs.com",
            ),
            344,
        ),
    ),
)


def entry(**extra: object) -> dict[str, object]:
    return {"company": "RTX", "industry": "Aerospace & Defense", "priority": "medium", **extra}


def test_seed_config_is_used_as_is_without_an_override() -> None:
    company, how = resolve(entry(), {"rtx": RTX_SEEN})
    assert how == "resolved" and company is not None
    assert company.provider_config == {
        "tenant": "globalhr",
        "host": "globalhr.wd5.myworkdayjobs.com",
        "site": "rec_rtx_ext_gateway",
    }
    assert company.support_status is SupportStatus.SUPPORTED


def test_an_override_tunes_how_the_board_is_read_and_keeps_where() -> None:
    company, _ = resolve(
        entry(provider_config_overrides={"queries": ["internship"]}), {"rtx": RTX_SEEN}
    )
    assert company is not None
    assert company.provider_config["queries"] == ["internship"]
    # Where the company posts still comes from the observed URLs.
    assert company.provider_config["tenant"] == "globalhr"
    assert company.provider_config["site"] == "rec_rtx_ext_gateway"


def test_editorial_notes_are_appended_to_the_evidence_not_replacing_it() -> None:
    company, _ = resolve(
        entry(notes="measured live: 'intern' is too loose here."), {"rtx": RTX_SEEN}
    )
    assert company is not None
    assert company.notes.startswith("config recovered from 344 observed posting URL(s)")
    assert company.notes.endswith("measured live: 'intern' is too loose here.")
