"""Level 6 - the jobs API (PRD §24), including the deep-link target.

Routing is framework-free, so these tests call it directly; a second, smaller set
drives the real API Gateway event shape through the Lambda adapter, and a third
starts the actual local HTTP server so the PWA's Playwright tests have something
proven to talk to.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

import pytest

from jobmonitor.api.lambda_handler import handler as api_gateway_handler
from jobmonitor.api.lambda_handler import to_request
from jobmonitor.api.local_server import build_local_api, seed_demo_data, serve
from jobmonitor.api.routes import Api, Request
from jobmonitor.models.company import load_default_registry
from jobmonitor.models.health import ScraperHealth, ScraperStatus
from jobmonitor.models.job import Job
from jobmonitor.storage.memory import (
    InMemoryDeviceRepository,
    InMemoryHealthRepository,
    InMemoryJobRepository,
)

pytestmark = pytest.mark.integration

T0 = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
WRITE_TOKEN = "test-write-token"


def intern(identifier: str, company: str = "Stripe", **kwargs: object) -> Job:
    payload: dict[str, object] = {
        "company": company,
        "title": "Software Engineer Intern",
        "url": f"https://stripe.com/jobs/listing/swe/{identifier}",
        "source": "greenhouse",
        "external_id": identifier,
        "location": "San Francisco, CA",
        "description": "Build payments infrastructure.",
    }
    payload.update(kwargs)
    return Job(**payload)  # type: ignore[arg-type]


@pytest.fixture
def api() -> Api:
    repository = InMemoryJobRepository()
    health = InMemoryHealthRepository()
    for index in range(5):
        repository.upsert(
            intern(str(index), title=f"Software Engineer Intern {index}"),
            relevance_score=70 + index,
            now=T0 + timedelta(minutes=index),
        )
    repository.upsert(
        intern("other", company="Figma"), relevance_score=40, now=T0 + timedelta(minutes=9)
    )
    health.record(
        ScraperHealth(
            company="Stripe",
            provider="greenhouse",
            status=ScraperStatus.SUCCESS,
            timestamp=T0,
            jobs_found=5,
        )
    )
    health.record(
        ScraperHealth(
            company="BadCo",
            provider="custom",
            status=ScraperStatus.FAILED,
            timestamp=T0,
            error_type="HTTPError",
            error="403",
        )
    )
    return Api(
        repository=repository,
        health_repository=health,
        device_repository=InMemoryDeviceRepository(),
        registry=load_default_registry(),
        write_token=WRITE_TOKEN,
    )


def get(api: Api, path: str, **query: str) -> tuple[int, Any]:
    response = api.handle(Request(method="GET", path=path, query=query))
    return response.status, json.loads(response.json())


class TestListJobs:
    def test_returns_recent_jobs_newest_first(self, api: Api) -> None:
        status, body = get(api, "/jobs")
        assert status == 200
        assert body["count"] == 6
        titles = [job["title"] for job in body["jobs"]]
        assert titles[0] == "Software Engineer Intern"  # the Figma one, newest
        assert body["jobs"][1]["title"] == "Software Engineer Intern 4"

    def test_recent_alias_behaves_the_same(self, api: Api) -> None:
        assert get(api, "/jobs/recent")[1]["count"] == get(api, "/jobs")[1]["count"]

    def test_limit_is_respected_and_capped(self, api: Api) -> None:
        assert get(api, "/jobs", limit="2")[1]["count"] == 2
        assert get(api, "/jobs", limit="99999")[1]["limit"] == 200

    def test_filter_by_company(self, api: Api) -> None:
        status, body = get(api, "/jobs", company="Figma")
        assert status == 200
        assert {job["company"] for job in body["jobs"]} == {"Figma"}

    def test_filter_by_minimum_score(self, api: Api) -> None:
        status, body = get(api, "/jobs", min_score="72")
        assert status == 200
        assert all(job["relevance_score"] >= 72 for job in body["jobs"])

    def test_filter_by_hours(self, api: Api) -> None:
        status, body = get(api, "/jobs", hours="0.001")
        assert status == 200
        assert body["count"] == 0  # everything was inserted "now" minus minutes

    def test_filter_by_since_timestamp(self, api: Api) -> None:
        status, body = get(api, "/jobs", since="2020-01-01T00:00:00Z")
        assert status == 200
        assert body["count"] == 6

    @pytest.mark.parametrize(
        ("query", "message"),
        [
            ({"limit": "abc"}, "must be an integer"),
            ({"limit": "0"}, "must be >= 1"),
            ({"hours": "soon"}, "must be a number"),
            ({"since": "yesterday"}, "ISO-8601"),
            ({"min_score": "high"}, "must be an integer"),
        ],
    )
    def test_invalid_query_parameters_are_rejected(
        self, api: Api, query: dict[str, str], message: str
    ) -> None:
        status, body = get(api, "/jobs", **query)
        assert status == 400
        assert message in body["error"]["message"]

    def test_the_response_exposes_no_internal_fields(self, api: Api) -> None:
        job = get(api, "/jobs")[1]["jobs"][0]
        for internal in ("content_hash", "notification_pending", "times_seen", "feed"):
            assert internal not in job

    def test_the_application_url_is_present_and_absolute(self, api: Api) -> None:
        for job in get(api, "/jobs")[1]["jobs"]:
            assert job["url"].startswith("https://")

    def test_an_empty_store_returns_an_empty_list_not_an_error(self) -> None:
        empty = Api(repository=InMemoryJobRepository())
        status, body = get(empty, "/jobs")
        assert status == 200
        assert body == {"count": 0, "limit": 50, "jobs": []}


class TestGetJob:
    def test_returns_one_job_by_id(self, api: Api) -> None:
        job_id = get(api, "/jobs")[1]["jobs"][0]["job_id"]
        status, body = get(api, f"/jobs/{quote(job_id, safe='')}")
        assert status == 200
        assert body["job"]["job_id"] == job_id

    def test_a_percent_encoded_id_from_a_deep_link_resolves(self, api: Api) -> None:
        """The exact path a notification tap produces."""
        job_id = "stripe:1"
        status, body = get(api, f"/jobs/{quote(job_id, safe='')}")
        assert status == 200
        assert body["job"]["job_id"] == job_id
        assert body["job"]["url"] == "https://stripe.com/jobs/listing/swe/1"

    def test_an_unknown_id_is_404(self, api: Api) -> None:
        status, body = get(api, "/jobs/does-not-exist")
        assert status == 404
        assert body["error"]["code"] == "not_found"

    def test_the_detail_payload_has_everything_the_detail_screen_needs(self, api: Api) -> None:
        body = get(api, f"/jobs/{quote('stripe:1', safe='')}")[1]["job"]
        for field in (
            "company",
            "title",
            "location",
            "url",
            "date_posted",
            "first_seen",
            "description",
            "relevance_score",
        ):
            assert field in body


class TestDeviceRegistration:
    def _post(self, api: Api, payload: dict[str, Any], *, token: str | None = WRITE_TOKEN):
        headers = {"X-Api-Token": token} if token else {}
        return api.handle(
            Request(
                method="POST",
                path="/devices/register",
                headers=headers,
                body=json.dumps(payload),
            )
        )

    def test_registers_a_device(self, api: Api) -> None:
        response = self._post(api, {"device_id": "phone-1", "token": "sub-token"})
        assert response.status == 201
        assert api.device_repository is not None
        assert [d.device_id for d in api.device_repository.enabled_devices()] == ["phone-1"]

    def test_the_push_token_is_never_echoed_back(self, api: Api) -> None:
        response = self._post(api, {"device_id": "phone-1", "token": "secret-subscription"})
        assert "secret-subscription" not in response.json()

    def test_requires_the_write_token(self, api: Api) -> None:
        response = self._post(api, {"device_id": "p", "token": "t"}, token=None)
        assert response.status == 401

    def test_rejects_a_wrong_write_token(self, api: Api) -> None:
        response = self._post(api, {"device_id": "p", "token": "t"}, token="guess")
        assert response.status == 401

    def test_writes_are_refused_when_no_token_is_configured(self) -> None:
        # Fails closed: an unconfigured deployment is not an open one.
        open_api = Api(
            repository=InMemoryJobRepository(), device_repository=InMemoryDeviceRepository()
        )
        response = self._post(open_api, {"device_id": "p", "token": "t"})
        assert response.status == 401
        assert "API_WRITE_TOKEN" in response.json()

    @pytest.mark.parametrize(
        ("payload", "message"),
        [({}, "device_id is required"), ({"device_id": "p"}, "token is required")],
    )
    def test_validates_the_payload(self, api: Api, payload: dict[str, Any], message: str) -> None:
        response = self._post(api, payload)
        assert response.status == 400
        assert message in response.json()

    def test_a_malformed_body_is_a_400_not_a_500(self, api: Api) -> None:
        response = api.handle(
            Request(
                method="POST",
                path="/devices/register",
                headers={"X-Api-Token": WRITE_TOKEN},
                body="{not json",
            )
        )
        assert response.status == 400

    def test_reregistration_is_idempotent(self, api: Api) -> None:
        self._post(api, {"device_id": "phone-1", "token": "a"})
        self._post(api, {"device_id": "phone-1", "token": "b"})
        assert api.device_repository is not None
        assert len(api.device_repository.enabled_devices()) == 1

    def test_unregister(self, api: Api) -> None:
        self._post(api, {"device_id": "phone-1", "token": "a"})
        response = api.handle(
            Request(method="DELETE", path="/devices/phone-1", headers={"X-Api-Token": WRITE_TOKEN})
        )
        assert response.status == 204
        assert api.device_repository is not None
        assert api.device_repository.enabled_devices() == []

    def test_unregister_requires_the_token(self, api: Api) -> None:
        assert api.handle(Request(method="DELETE", path="/devices/x")).status == 401


class TestHealthAndMeta:
    def test_health_reports_every_scraper_and_counts_failures(self, api: Api) -> None:
        status, body = get(api, "/health")
        assert status == 200
        assert body["scrapers_total"] == 2
        assert body["scrapers_failing"] == 1
        failing = next(s for s in body["scrapers"] if s["status"] == "failed")
        assert failing["company"] == "BadCo"
        assert failing["error"] == "403"

    def test_health_without_a_repository_degrades_gracefully(self) -> None:
        status, body = get(Api(repository=InMemoryJobRepository()), "/health")
        assert status == 200
        assert body["scrapers"] == []

    def test_meta_reports_registry_coverage(self, api: Api) -> None:
        status, body = get(api, "/meta")
        assert status == 200
        assert body["jobs_stored"] == 6
        assert 100 <= body["companies"]["pollable"] <= 150
        assert body["companies"]["by_provider"]["greenhouse"] > 0


class TestRouting:
    def test_index_lists_the_endpoints(self, api: Api) -> None:
        status, body = get(api, "/")
        assert status == 200
        assert "GET /jobs" in body["endpoints"]

    def test_unknown_paths_are_404(self, api: Api) -> None:
        assert get(api, "/nope")[0] == 404

    def test_a_wrong_method_is_405_not_404(self, api: Api) -> None:
        response = api.handle(Request(method="POST", path="/jobs"))
        assert response.status == 405

    def test_trailing_slashes_are_tolerated(self, api: Api) -> None:
        assert get(api, "/jobs/")[0] == 200

    def test_options_preflight_succeeds_with_cors_headers(self, api: Api) -> None:
        response = api.handle(Request(method="OPTIONS", path="/jobs"))
        assert response.status == 204
        assert response.all_headers["Access-Control-Allow-Origin"] == "*"

    def test_responses_are_not_cached(self, api: Api) -> None:
        response = api.handle(Request(method="GET", path="/jobs"))
        assert response.all_headers["Cache-Control"] == "no-store"


class TestApiGatewayAdapter:
    def _event(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        return {
            "version": "2.0",
            "rawPath": path,
            "requestContext": {"http": {"method": method, "path": path}, "stage": "$default"},
            **kwargs,
        }

    def test_translates_a_v2_event(self, api: Api) -> None:
        result = api_gateway_handler(self._event("GET", "/jobs"), api=api)
        assert result["statusCode"] == 200
        assert json.loads(result["body"])["count"] == 6
        assert result["headers"]["Content-Type"] == "application/json"

    def test_translates_a_v1_event(self, api: Api) -> None:
        event = {"httpMethod": "GET", "path": "/jobs", "requestContext": {}}
        assert api_gateway_handler(event, api=api)["statusCode"] == 200

    def test_query_parameters_are_forwarded(self, api: Api) -> None:
        event = self._event("GET", "/jobs", queryStringParameters={"limit": "2"})
        assert json.loads(api_gateway_handler(event, api=api)["body"])["count"] == 2

    def test_a_base64_body_is_decoded(self, api: Api) -> None:
        import base64

        body = json.dumps({"device_id": "phone-1", "token": "t"})
        event = self._event(
            "POST",
            "/devices/register",
            headers={"X-Api-Token": WRITE_TOKEN},
            body=base64.b64encode(body.encode()).decode(),
            isBase64Encoded=True,
        )
        assert api_gateway_handler(event, api=api)["statusCode"] == 201

    def test_a_stage_prefix_is_stripped(self) -> None:
        event = {
            "version": "2.0",
            "rawPath": "/prod/jobs",
            "requestContext": {"http": {"method": "GET", "path": "/prod/jobs"}, "stage": "prod"},
        }
        assert to_request(event).path == "/jobs"

    def test_a_204_carries_no_body(self, api: Api) -> None:
        result = api_gateway_handler(self._event("OPTIONS", "/jobs"), api=api)
        assert result["statusCode"] == 204
        assert "body" not in result

    def test_an_empty_event_does_not_crash(self, api: Api) -> None:
        assert api_gateway_handler({}, api=api)["statusCode"] == 200


class TestLocalServer:
    """The server the PWA's browser tests actually talk to."""

    @pytest.fixture
    def running(self) -> Iterator[str]:
        repository = InMemoryJobRepository()
        seed_demo_data(repository)
        api = build_local_api(repository=repository, write_token=WRITE_TOKEN)
        server, _thread = serve(api, host="127.0.0.1", port=0)
        port = server.server_address[1]
        try:
            yield f"http://127.0.0.1:{port}"
        finally:
            server.shutdown()
            server.server_close()

    def _fetch(self, url: str, **kwargs: Any) -> tuple[int, Any]:
        request = urllib.request.Request(url, **kwargs)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                raw = response.read()
                return response.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            return exc.code, (json.loads(raw) if raw else None)

    def test_serves_the_seeded_jobs_over_http(self, running: str) -> None:
        status, body = self._fetch(f"{running}/jobs")
        assert status == 200
        assert body["count"] == 5
        assert {job["company"] for job in body["jobs"]} >= {"Anthropic", "Jane Street"}

    def test_serves_a_single_job_over_http(self, running: str) -> None:
        job_id = self._fetch(f"{running}/jobs")[1]["jobs"][0]["job_id"]
        status, body = self._fetch(f"{running}/jobs/{quote(job_id, safe='')}")
        assert status == 200
        assert body["job"]["job_id"] == job_id

    def test_serves_cors_headers_for_the_pwa_origin(self, running: str) -> None:
        with urllib.request.urlopen(f"{running}/jobs", timeout=10) as response:
            assert response.headers["Access-Control-Allow-Origin"] == "*"

    def test_404_over_http(self, running: str) -> None:
        assert self._fetch(f"{running}/jobs/nope")[0] == 404

    def test_device_registration_over_http(self, running: str) -> None:
        status, _body = self._fetch(
            f"{running}/devices/register",
            data=json.dumps({"device_id": "phone-1", "token": "t"}).encode(),
            headers={"Content-Type": "application/json", "X-Api-Token": WRITE_TOKEN},
            method="POST",
        )
        assert status == 201

    def test_meta_over_http(self, running: str) -> None:
        status, body = self._fetch(f"{running}/meta")
        assert status == 200
        assert body["jobs_stored"] == 5
