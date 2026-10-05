"""The developer CLI (PRD §25 "create a simple scraper-health view or command").

Every command runs in process against in-memory storage - no ``--url``, so no
AWS, no LocalStack, no network. The point is that the observability surface the
PRD asks for is exercised rather than merely present, including the two commands
that exist to debug the last hop to a phone (``devices`` and ``push-test``).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from jobmonitor import cli
from jobmonitor.models.health import ScraperHealth, ScraperStatus
from jobmonitor.models.job import Job
from jobmonitor.models.record import DeviceRegistration
from jobmonitor.storage.memory import (
    InMemoryDeviceRepository,
    InMemoryHealthRepository,
    InMemoryJobRepository,
)

pytestmark = pytest.mark.integration

T0 = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


@pytest.fixture
def stores(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[InMemoryJobRepository, InMemoryHealthRepository, InMemoryDeviceRepository]:
    """Pin the CLI to one set of in-memory stores for the whole command."""
    jobs = InMemoryJobRepository()
    health = InMemoryHealthRepository()
    devices = InMemoryDeviceRepository()
    monkeypatch.setattr(cli, "_repositories", lambda url: (jobs, health))
    monkeypatch.setattr(cli, "_device_repository", lambda url: devices)
    return jobs, health, devices


def run(*argv: str) -> int:
    return cli.main(list(argv))


def a_job(identifier: str = "1") -> Job:
    return Job(
        company="Stripe",
        title="Software Engineer Intern",
        url=f"https://stripe.com/jobs/listing/swe-intern/{identifier}",
        source="greenhouse",
        external_id=identifier,
        location="San Francisco",
    )


class TestHealthView:
    def test_says_so_when_there_is_nothing_yet(
        self, stores: tuple, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run("health") == 0
        assert "run a poll first" in capsys.readouterr().out

    def test_renders_the_prd_table(self, stores: tuple, capsys: pytest.CaptureFixture[str]) -> None:
        _jobs, health, _devices = stores
        health.record_many(
            [
                ScraperHealth(
                    company="Stripe",
                    provider="greenhouse",
                    status=ScraperStatus.SUCCESS,
                    timestamp=T0,
                    jobs_found=32,
                    new_jobs=1,
                ),
                ScraperHealth(
                    company="Company X",
                    provider="custom",
                    status=ScraperStatus.BLOCKED,
                    timestamp=T0,
                    error="403",
                    error_type="AccessBlocked",
                    attempts=3,
                ),
            ]
        )
        assert run("health") == 0
        out = capsys.readouterr().out
        assert "Stripe" in out and "OK" in out
        assert "Company X" in out and "BLOCKED" in out and "403" in out
        assert "2 scraper(s), 1 failing" in out

    def test_each_source_of_a_company_is_named(
        self, stores: tuple, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _jobs, health, _devices = stores
        health.record_many(
            [
                ScraperHealth(
                    company="Anthropic",
                    provider="greenhouse",
                    status=ScraperStatus.SUCCESS,
                    timestamp=T0,
                    jobs_found=40,
                ),
                ScraperHealth(
                    company="Anthropic",
                    provider="event_page:campus",
                    status=ScraperStatus.FAILED,
                    timestamp=T0,
                    error="maintenance page",
                    error_type="ParseError",
                ),
            ]
        )
        assert run("health") == 0
        lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
        assert "event_page:campus" in lines[0] and "FAIL" in lines[0]  # failures first
        assert "greenhouse" in lines[1] and "OK" in lines[1]
        assert "2 scraper(s), 1 failing" in lines[-1]

    def test_strict_exits_non_zero_when_a_scraper_is_failing(self, stores: tuple) -> None:
        _jobs, health, _devices = stores
        health.record(
            ScraperHealth(
                company="Company Y",
                provider="custom",
                status=ScraperStatus.FAILED,
                timestamp=T0,
                error="parser mismatch",
            )
        )
        assert run("health", "--strict") == 1

    def test_json_output_is_machine_readable(
        self, stores: tuple, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _jobs, health, _devices = stores
        health.record(
            ScraperHealth(
                company="Stripe",
                provider="greenhouse",
                status=ScraperStatus.SUCCESS,
                timestamp=T0,
            )
        )
        assert run("health", "--json") == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload[0]["company"] == "Stripe"


class TestJobsView:
    def test_lists_stored_jobs_with_their_links(
        self, stores: tuple, capsys: pytest.CaptureFixture[str]
    ) -> None:
        jobs, _health, _devices = stores
        jobs.upsert(a_job(), relevance_score=88, now=T0)
        assert run("jobs") == 0
        out = capsys.readouterr().out
        assert "Stripe - Software Engineer Intern" in out
        assert "https://stripe.com/jobs/listing/swe-intern/1" in out
        assert "[not yet notified]" in out

    def test_json_output_matches_the_api_shape(
        self, stores: tuple, capsys: pytest.CaptureFixture[str]
    ) -> None:
        jobs, _health, _devices = stores
        jobs.upsert(a_job(), now=T0)
        assert run("jobs", "--json") == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload[0]["company"] == "Stripe"
        assert payload[0]["url"].startswith("https://")

    def test_empty_is_not_an_error(self, stores: tuple, capsys: pytest.CaptureFixture[str]) -> None:
        assert run("jobs") == 0
        assert "no jobs stored yet" in capsys.readouterr().out


class TestDevicesView:
    def test_explains_how_to_get_a_device_registered(
        self, stores: tuple, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run("devices") == 0
        assert "grant notification permission" in capsys.readouterr().out

    def test_lists_a_registered_phone_without_printing_the_whole_token(
        self, stores: tuple, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _jobs, _health, devices = stores
        devices.register(
            DeviceRegistration(
                device_id="ios-Test-Phone-abcdef",
                token="ExponentPushToken[aaaaaaaaaaaaaaaaaaaaaaaa]",
                transport="expo",
            )
        )
        assert run("devices") == 0
        out = capsys.readouterr().out
        assert "ios-Test-Phone-abcdef" in out
        assert "expo" in out
        assert "ExponentPushToken[aaaa" in out
        # Truncated: enough to tell two handsets apart, not enough to reuse.
        assert "ExponentPushToken[aaaaaaaaaaaaaaaaaaaaaaaa]" not in out

    def test_json_output_lists_devices(
        self, stores: tuple, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _jobs, _health, devices = stores
        devices.register(
            DeviceRegistration(device_id="phone", token="ExponentPushToken[x]", transport="expo")
        )
        assert run("devices", "--json") == 0
        assert json.loads(capsys.readouterr().out)[0]["device_id"] == "phone"


class TestPushTest:
    def test_refuses_when_no_device_is_registered_and_the_transport_needs_one(
        self, stores: tuple, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("PUSH_TRANSPORT", "expo")
        jobs, _health, _devices = stores
        jobs.upsert(a_job(), now=T0)
        assert run("push-test") == 2
        assert "no devices registered" in capsys.readouterr().err

    def test_refuses_when_there_is_no_job_to_send(
        self, stores: tuple, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("PUSH_TRANSPORT", "memory")
        assert run("push-test") == 2
        assert "no jobs stored yet" in capsys.readouterr().err

    def test_sends_one_alert_and_prints_the_deep_link(
        self, stores: tuple, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("PUSH_TRANSPORT", "console")
        monkeypatch.setenv("APP_BASE_URL", "https://app.test")
        jobs, _health, devices = stores
        record = jobs.upsert(a_job(), relevance_score=90, now=T0).record
        devices.register(
            DeviceRegistration(device_id="phone", token="ExponentPushToken[x]", transport="expo")
        )
        assert run("push-test") == 0
        out = capsys.readouterr().out
        assert "sending via console to 1 device(s)" in out
        # The deep link is the whole point of the command: it is what the user
        # taps, and what must resolve to this job.
        assert record.job_id.replace(":", "%3A") in out
        assert "Tap the notification" in out

    def test_reports_a_transport_failure_rather_than_claiming_success(
        self, stores: tuple, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("PUSH_TRANSPORT", "expo")
        # No endpoint reachable and no Expo tokens configured: the request cannot
        # succeed, and the command must say so with a non-zero exit.
        monkeypatch.setenv("EXPO_PUSH_API_URL", "https://127.0.0.1:1/send")
        monkeypatch.setenv("HTTP_MAX_ATTEMPTS", "1")
        monkeypatch.setenv("HTTP_BACKOFF_BASE_SECONDS", "0")
        monkeypatch.setenv("HTTP_TIMEOUT_SECONDS", "0.2")
        jobs, _health, devices = stores
        jobs.upsert(a_job(), now=T0)
        devices.register(
            DeviceRegistration(device_id="phone", token="ExponentPushToken[x]", transport="expo")
        )
        assert run("push-test") == 1
        assert "FAILED" in capsys.readouterr().err

    def test_a_sample_job_needs_nothing_stored(
        self, stores: tuple, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`push-test --sample` checks a fresh phone set-up before any poll ran."""
        monkeypatch.setenv("PUSH_TRANSPORT", "console")
        _jobs, _health, devices = stores
        devices.register(DeviceRegistration(device_id="phone", token="t", transport="expo"))
        assert run("push-test", "--sample") == 0
        assert "Test alert - new internship" in capsys.readouterr().out


class TestDigest:
    def test_emails_the_previous_hour(
        self, stores: tuple, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("EMAIL_TRANSPORT", "console")
        jobs, _health, _devices = stores
        top_of_hour = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
        jobs.upsert(a_job(), relevance_score=90, now=top_of_hour - timedelta(minutes=30))
        assert run("digest") == 0
        out = capsys.readouterr().out
        assert "[Internships] 1 new internship - Stripe" in out
        assert '"sent": true' in out

    def test_an_empty_hour_sends_nothing(
        self, stores: tuple, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("EMAIL_TRANSPORT", "console")
        assert run("digest") == 0
        out = capsys.readouterr().out
        assert "=== EMAIL" not in out
        assert '"skipped_empty": true' in out


class TestCoverage:
    def test_summarizes_the_real_registry(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert run("coverage") == 0
        out = capsys.readouterr().out
        assert "Total companies in the registry:" in out
        assert "By provider:" in out
        assert "greenhouse" in out


class TestParser:
    def test_a_command_is_required(self) -> None:
        with pytest.raises(SystemExit):
            run()

    def test_an_unknown_command_is_rejected(self) -> None:
        with pytest.raises(SystemExit):
            run("teleport")


class TestPointingAtAws:
    """`--url` must never plant fake credentials over a real AWS profile."""

    @pytest.fixture(autouse=True)
    def clean_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for key in ("AWS_ENDPOINT_URL", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
            monkeypatch.delenv(key, raising=False)

    def test_localstack_gets_fake_credentials(self) -> None:
        import os

        cli._point_at("http://localhost:4566")
        assert os.environ["AWS_ENDPOINT_URL"] == "http://localhost:4566"
        assert os.environ["AWS_ACCESS_KEY_ID"] == "test"

    def test_real_aws_uses_your_own_credentials_and_endpoints(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import os

        monkeypatch.setenv("AWS_ENDPOINT_URL", "http://localhost:4566")
        cli._point_at("aws")
        assert "AWS_ENDPOINT_URL" not in os.environ
        assert "AWS_ACCESS_KEY_ID" not in os.environ

    def test_a_non_local_endpoint_gets_no_fake_credentials(self) -> None:
        import os

        cli._point_at("https://dynamodb.us-east-1.amazonaws.com")
        assert "AWS_ACCESS_KEY_ID" not in os.environ
