"""Level 1/2 - the retry policy: backoff, jitter, Retry-After, error classes.

These are the PRD §14 "representative sequences" (``500 500 200`` and
``429 + Retry-After``) exercised against a scripted transport, so they assert the
exact behaviour with no network and no real waiting.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest
from tests.support.http import FakeTransport, RecordingSleeper, ScriptedResponse

from jobmonitor.config import HttpSettings
from jobmonitor.errors import (
    AccessBlocked,
    HttpStatusError,
    NetworkError,
    ParseError,
    RetryBudgetExhausted,
)
from jobmonitor.http import (
    MAX_RETRY_AFTER_SECONDS,
    HttpClient,
    HttpResponse,
    parse_retry_after,
)

URL = "https://api.example.com/jobs"


def build(
    transport: FakeTransport, *, sleeper: RecordingSleeper | None = None, **overrides: object
) -> tuple[HttpClient, RecordingSleeper]:
    sleeper = sleeper or RecordingSleeper()
    defaults: dict[str, object] = {
        "timeout_seconds": 1.0,
        "max_attempts": 3,
        "backoff_base_seconds": 0.5,
        "backoff_max_seconds": 8.0,
    }
    defaults.update(overrides)
    settings = HttpSettings(**defaults)  # type: ignore[arg-type]
    return (
        HttpClient(settings, transport=transport, sleep=sleeper, rng=random.Random(7)),
        sleeper,
    )


class TestParseRetryAfter:
    @pytest.mark.parametrize(("raw", "expected"), [("5", 5.0), ("0", 0.0), ("2.5", 2.5)])
    def test_delta_seconds(self, raw: str, expected: float) -> None:
        assert parse_retry_after(raw) == expected

    def test_http_date(self) -> None:
        now = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
        later = now + timedelta(seconds=30)
        header = later.strftime("%a, %d %b %Y %H:%M:%S GMT")
        assert parse_retry_after(header, now=now) == pytest.approx(30.0, abs=1.0)

    def test_past_http_date_clamps_to_zero(self) -> None:
        now = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
        header = (now - timedelta(seconds=60)).strftime("%a, %d %b %Y %H:%M:%S GMT")
        assert parse_retry_after(header, now=now) == 0.0

    def test_negative_seconds_clamp_to_zero(self) -> None:
        assert parse_retry_after("-10") == 0.0

    @pytest.mark.parametrize("raw", [None, "", "   ", "soon", "next tuesday"])
    def test_unusable_values_return_none(self, raw: str | None) -> None:
        assert parse_retry_after(raw) is None


class TestSuccessPath:
    def test_returns_json_on_first_try(self) -> None:
        transport = FakeTransport([ScriptedResponse.json({"jobs": [1, 2]})])
        client, sleeper = build(transport)
        assert client.get_json(URL) == {"jobs": [1, 2]}
        assert transport.call_count == 1
        assert sleeper.delays == []

    def test_sends_configured_user_agent(self) -> None:
        transport = FakeTransport([ScriptedResponse.json({})])
        client, _ = build(transport)
        client.request(URL)
        assert transport.last_request.headers["User-Agent"] == HttpSettings().user_agent

    def test_caller_headers_override_defaults(self) -> None:
        transport = FakeTransport([ScriptedResponse.json({})])
        client, _ = build(transport)
        client.request(URL, headers={"Accept": "text/csv", "X-Trace": "1"})
        assert transport.last_request.headers["Accept"] == "text/csv"
        assert transport.last_request.headers["X-Trace"] == "1"

    def test_post_json_sets_content_type_and_body(self) -> None:
        transport = FakeTransport([ScriptedResponse.json({"ok": True})])
        client, _ = build(transport)
        assert client.post_json(URL, {"limit": 20}) == {"ok": True}
        request = transport.last_request
        assert request.method == "POST"
        assert request.headers["Content-Type"] == "application/json"
        assert transport.bodies() == [{"limit": 20}]

    def test_body_and_json_body_together_is_a_programming_error(self) -> None:
        client, _ = build(FakeTransport(always=ScriptedResponse.json({})))
        with pytest.raises(ValueError, match="either body or json_body"):
            client.request(URL, body=b"x", json_body={"a": 1})

    def test_2xx_other_than_200_is_success(self) -> None:
        transport = FakeTransport([ScriptedResponse(status=204, body=b"")])
        client, _ = build(transport)
        assert client.request(URL).status == 204


class TestTransientRecovery:
    def test_500_500_200_succeeds_after_retry(self) -> None:
        transport = FakeTransport(
            [
                ScriptedResponse.error(500),
                ScriptedResponse.error(500),
                ScriptedResponse.json({"jobs": []}),
            ]
        )
        client, sleeper = build(transport)
        assert client.get_json(URL) == {"jobs": []}
        assert transport.call_count == 3
        assert client.log.statuses == [500, 500, 200]
        assert len(sleeper.delays) == 2

    def test_502_503_504_are_retried(self) -> None:
        for status in (502, 503, 504):
            transport = FakeTransport([ScriptedResponse.error(status), ScriptedResponse.json({})])
            client, _ = build(transport)
            client.request(URL)
            assert transport.call_count == 2, status

    def test_network_errors_are_retried(self) -> None:
        transport = FakeTransport(
            [
                ScriptedResponse.boom(NetworkError("connection reset")),
                ScriptedResponse.boom(NetworkError("timed out")),
                ScriptedResponse.json({"ok": 1}),
            ]
        )
        client, _ = build(transport)
        assert client.get_json(URL) == {"ok": 1}
        assert client.log.errors == [
            "NetworkError: connection reset",
            "NetworkError: timed out",
        ]

    def test_exhausted_budget_raises_with_the_last_error(self) -> None:
        transport = FakeTransport(always=ScriptedResponse.error(503))
        client, sleeper = build(transport)
        with pytest.raises(RetryBudgetExhausted) as excinfo:
            client.request(URL)
        assert excinfo.value.attempts == 3
        assert transport.call_count == 3
        # No sleep after the final failed attempt.
        assert len(sleeper.delays) == 2

    def test_max_attempts_of_one_does_not_retry(self) -> None:
        transport = FakeTransport(always=ScriptedResponse.error(500))
        client, sleeper = build(transport, max_attempts=1)
        with pytest.raises(RetryBudgetExhausted):
            client.request(URL)
        assert transport.call_count == 1
        assert sleeper.delays == []


class TestRateLimiting:
    def test_429_with_retry_after_waits_exactly_that_long(self) -> None:
        transport = FakeTransport(
            [
                ScriptedResponse.error(429, headers={"Retry-After": "2"}),
                ScriptedResponse.json({"jobs": []}),
            ]
        )
        client, sleeper = build(transport)
        assert client.get_json(URL) == {"jobs": []}
        # Retry-After wins over our own backoff: never hammer a source that asked us to wait.
        assert sleeper.delays == [2.0]

    def test_429_without_retry_after_uses_jittered_backoff(self) -> None:
        transport = FakeTransport(
            [ScriptedResponse.error(429), ScriptedResponse.json({"jobs": []})]
        )
        client, sleeper = build(transport)
        client.request(URL)
        assert len(sleeper.delays) == 1
        assert 0.0 <= sleeper.delays[0] <= 0.5

    def test_retry_after_is_capped(self) -> None:
        transport = FakeTransport(
            [
                ScriptedResponse.error(429, headers={"Retry-After": "100000"}),
                ScriptedResponse.json({}),
            ]
        )
        client, sleeper = build(transport)
        client.request(URL)
        assert sleeper.delays == [MAX_RETRY_AFTER_SECONDS]

    def test_retry_after_header_lookup_is_case_insensitive(self) -> None:
        transport = FakeTransport(
            [ScriptedResponse.error(429, headers={"retry-after": "3"}), ScriptedResponse.json({})]
        )
        client, sleeper = build(transport)
        client.request(URL)
        assert sleeper.delays == [3.0]

    def test_persistent_429_exhausts_the_budget(self) -> None:
        transport = FakeTransport(always=ScriptedResponse.error(429, headers={"Retry-After": "1"}))
        client, sleeper = build(transport)
        with pytest.raises(RetryBudgetExhausted):
            client.request(URL)
        assert transport.call_count == 3
        assert sleeper.delays == [1.0, 1.0]


class TestPermanentFailures:
    @pytest.mark.parametrize("status", [401, 403])
    def test_access_denied_is_not_retried(self, status: int) -> None:
        transport = FakeTransport(always=ScriptedResponse.error(status))
        client, _ = build(transport)
        with pytest.raises(AccessBlocked, match="refused automated access"):
            client.request(URL)
        # Retrying a bot wall is both useless and rude.
        assert transport.call_count == 1

    @pytest.mark.parametrize("status", [400, 404, 410, 422])
    def test_client_errors_are_not_retried(self, status: int) -> None:
        transport = FakeTransport(always=ScriptedResponse.error(status))
        client, _ = build(transport)
        with pytest.raises(HttpStatusError) as excinfo:
            client.request(URL)
        assert excinfo.value.status == status
        assert transport.call_count == 1

    def test_408_and_425_are_retried(self) -> None:
        for status in (408, 425):
            transport = FakeTransport([ScriptedResponse.error(status), ScriptedResponse.json({})])
            client, _ = build(transport)
            client.request(URL)
            assert transport.call_count == 2, status


class TestBackoffCalculation:
    def test_delay_grows_exponentially_within_the_jitter_envelope(self) -> None:
        client, _ = build(FakeTransport(always=ScriptedResponse.json({})))
        for attempt, ceiling in ((1, 0.5), (2, 1.0), (3, 2.0), (4, 4.0)):
            samples = [client.backoff_delay(attempt) for _ in range(50)]
            assert all(0.0 <= s <= ceiling for s in samples), attempt
            assert max(samples) > ceiling / 2  # jitter really does span the range

    def test_delay_is_capped_at_backoff_max(self) -> None:
        client, _ = build(FakeTransport(always=ScriptedResponse.json({})))
        assert all(client.backoff_delay(20) <= 8.0 for _ in range(50))

    def test_zero_base_disables_waiting(self) -> None:
        client, _ = build(FakeTransport(always=ScriptedResponse.json({})), backoff_base_seconds=0.0)
        assert client.backoff_delay(5) == 0.0

    def test_jitter_actually_varies(self) -> None:
        client, _ = build(FakeTransport(always=ScriptedResponse.json({})))
        assert len({client.backoff_delay(3) for _ in range(20)}) > 1

    def test_retry_after_overrides_computed_backoff(self) -> None:
        client, _ = build(FakeTransport(always=ScriptedResponse.json({})))
        assert client.backoff_delay(1, retry_after=7.0) == 7.0


class TestResponseHelpers:
    def test_json_parse_error_is_a_parse_error(self) -> None:
        transport = FakeTransport([ScriptedResponse.text("<html>nope</html>")])
        client, _ = build(transport)
        with pytest.raises(ParseError, match="not valid JSON"):
            client.get_json(URL)

    def test_empty_body_where_json_expected(self) -> None:
        transport = FakeTransport([ScriptedResponse(status=200, body=b"")])
        client, _ = build(transport)
        with pytest.raises(ParseError, match="empty response body"):
            client.get_json(URL)

    def test_text_decodes_leniently(self) -> None:
        response = HttpResponse(status=200, body=b"caf\xe9")
        assert "caf" in response.text()

    def test_header_lookup_is_case_insensitive(self) -> None:
        response = HttpResponse(status=200, body=b"", headers={"Content-Type": "application/json"})
        assert response.header("content-type") == "application/json"
        assert response.header("missing") is None


class TestAttemptLog:
    def test_log_records_statuses_sleeps_and_errors(self) -> None:
        transport = FakeTransport(
            [
                ScriptedResponse.boom(NetworkError("reset")),
                ScriptedResponse.error(500),
                ScriptedResponse.json({}),
            ]
        )
        client, _ = build(transport)
        client.request(URL)
        assert client.log.attempts == 3
        assert client.log.statuses == [500, 200]
        assert len(client.log.sleeps) == 2
        assert client.log.errors[0].startswith("NetworkError")
