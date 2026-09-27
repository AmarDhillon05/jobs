"""Live validation's verdicts (scripts/validate_companies.py + the registry build).

Two rules pinned here, both found when validation first ran for real:

* A transient failure (429, 5xx, timeout) is *inconclusive*. It must not demote a
  company: mapping it to research-needed would stop the company being polled -
  and Microsoft answered 429 during the first live run.
* Verdicts live in data/validation.json and are merged by the build. Writing
  companies.json in place made the build's --check (and so `make verify`) fail
  the moment validation ran.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from tests.conftest import make_company
from tests.support.http import FakeTransport, ScriptedResponse

from jobmonitor.config import HttpSettings
from jobmonitor.http import HttpClient
from jobmonitor.models.company import SupportStatus

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import build_company_registry
import validate_companies

pytestmark = pytest.mark.unit


def client(*responses: ScriptedResponse) -> HttpClient:
    return HttpClient(
        HttpSettings(max_attempts=2, backoff_base_seconds=0.0),
        transport=FakeTransport(always=responses[0])
        if len(responses) == 1
        else FakeTransport(list(responses)),
        sleep=lambda _s: None,
    )


GREENHOUSE_BOARD = {
    "jobs": [
        {"id": 1, "title": "Software Engineer Intern", "absolute_url": "https://x.test/jobs/1"}
    ]
}


class TestVerdicts:
    def test_a_working_board_is_supported(self) -> None:
        outcome = validate_companies.probe(
            make_company(), client(ScriptedResponse.json(GREENHOUSE_BOARD))
        )
        assert outcome.resolved is SupportStatus.SUPPORTED
        assert not outcome.inconclusive

    def test_a_404_is_research_needed(self) -> None:
        outcome = validate_companies.probe(make_company(), client(ScriptedResponse.error(404)))
        assert outcome.resolved is SupportStatus.RESEARCH_NEEDED
        assert not outcome.inconclusive

    def test_a_403_is_blocked(self) -> None:
        outcome = validate_companies.probe(make_company(), client(ScriptedResponse.error(403)))
        assert outcome.resolved is SupportStatus.BLOCKED

    @pytest.mark.parametrize("status", [429, 500, 503])
    def test_a_transient_failure_is_inconclusive_and_changes_nothing(self, status: int) -> None:
        company = make_company(support_status=SupportStatus.SUPPORTED)
        outcome = validate_companies.probe(company, client(ScriptedResponse.error(status)))
        assert outcome.inconclusive
        assert outcome.resolved is SupportStatus.SUPPORTED
        assert not outcome.changed

    def test_a_fallback_source_is_never_promoted_past_partial(self) -> None:
        company = make_company(provider="simplify_fallback", config={"company_names": ["TestCo"]})
        feed = [
            {
                "company_name": "TestCo",
                "title": "SWE Intern",
                "url": "https://x.test/1",
                "active": True,
                "is_visible": True,
                "id": "a",
            }
        ]
        outcome = validate_companies.probe(company, client(ScriptedResponse.json(feed)))
        assert outcome.resolved is SupportStatus.PARTIAL


def outcome(
    company: str, status: SupportStatus, *, inconclusive: bool = False
) -> validate_companies.Outcome:
    return validate_companies.Outcome(
        company=company,
        provider="greenhouse",
        previous=SupportStatus.SUPPORTED,
        resolved=status,
        jobs=3,
        malformed=0,
        duration_ms=10,
        error=None,
        inconclusive=inconclusive,
    )


class TestRecording:
    @pytest.fixture
    def validation_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        path = tmp_path / "validation.json"
        monkeypatch.setattr(validate_companies, "VALIDATION_PATH", path)
        monkeypatch.setattr(build_company_registry, "VALIDATION_PATH", path)
        return path

    def test_verdicts_are_merged_not_replaced(self, validation_file: Path) -> None:
        validate_companies.record_verdicts([outcome("A", SupportStatus.SUPPORTED)], "2026-09-27")
        validate_companies.record_verdicts([outcome("B", SupportStatus.BLOCKED)], "2026-09-28")
        recorded = json.loads(validation_file.read_text())["companies"]
        assert recorded["A"]["validated_on"] == "2026-09-27"
        assert recorded["B"]["status"] == "blocked"

    def test_an_inconclusive_run_never_overwrites_the_last_real_verdict(
        self, validation_file: Path
    ) -> None:
        validate_companies.record_verdicts([outcome("A", SupportStatus.SUPPORTED)], "2026-09-27")
        validate_companies.record_verdicts(
            [outcome("A", SupportStatus.SUPPORTED, inconclusive=True)], "2026-09-28"
        )
        recorded = json.loads(validation_file.read_text())["companies"]
        assert recorded["A"]["validated_on"] == "2026-09-27"

    def test_the_build_overlays_verdicts_and_dates(self, validation_file: Path) -> None:
        validate_companies.record_verdicts([outcome("TestCo", SupportStatus.BLOCKED)], "2026-09-27")
        [company] = build_company_registry.apply_validation([make_company()])
        assert company.support_status is SupportStatus.BLOCKED
        assert company.last_validated == "2026-09-27"

    def test_the_build_keeps_a_fallback_at_partial_whatever_was_recorded(
        self, validation_file: Path
    ) -> None:
        validate_companies.record_verdicts(
            [outcome("TestCo", SupportStatus.SUPPORTED)], "2026-09-27"
        )
        fallback = make_company(provider="simplify_fallback", config={"company_names": ["TestCo"]})
        [company] = build_company_registry.apply_validation([fallback])
        assert company.support_status is SupportStatus.PARTIAL

    def test_no_validation_file_leaves_the_registry_alone(self, validation_file: Path) -> None:
        company = make_company()
        assert build_company_registry.apply_validation([company]) == [company]


class TestPartialSearchFailures:
    def test_a_rate_limited_later_search_is_inconclusive(self) -> None:
        """Observed live: Microsoft's 'summer analyst' term hit a rate limit."""
        board = {
            "hits": 1,
            "jobs": [{"id_icims": "1", "title": "SWE Intern", "job_path": "/en/jobs/1"}],
        }
        responses = [ScriptedResponse.json(board)] + [ScriptedResponse.error(429)] * 2
        company = make_company(provider="amazon", config={"queries": ["intern", "summer analyst"]})
        outcome = validate_companies.probe(company, client(*responses))
        assert outcome.inconclusive
        assert outcome.resolved is company.support_status

    def test_a_malformed_board_is_still_a_real_verdict(self) -> None:
        board = {"jobs": [GREENHOUSE_BOARD["jobs"][0], {"id": 2, "title": "no url"}]}
        outcome = validate_companies.probe(make_company(), client(ScriptedResponse.json(board)))
        assert not outcome.inconclusive
        assert outcome.resolved is SupportStatus.PARTIAL
