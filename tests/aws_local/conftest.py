"""Fixtures for Level 7 - the architecture running on emulated AWS.

These tests talk to a LocalStack that ``make local`` has already provisioned, and
they skip (rather than fail) when it is not there, so the default test run stays
hermetic. ``make e2e`` boots it, provisions it, runs them and tears it down.

Everything here polls with a deadline instead of sleeping a guessed interval:
Lambda cold starts and SQS delivery are genuinely asynchronous, and a fixed sleep
either makes the suite slow or makes it flaky.
"""

from __future__ import annotations

import contextlib
import json
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any, TypeVar

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from jobmonitor.models.company import Company, Priority, SupportStatus  # noqa: E402
from local_provision import Resources, client  # noqa: E402

T = TypeVar("T")

#: Generous enough for a Lambda cold start in a container, short enough that a
#: genuine failure is reported rather than waited out.
DEFAULT_TIMEOUT = 90.0
POLL_INTERVAL = 1.5


def wait_for(probe: Callable[[], T | None], *, what: str, timeout: float = DEFAULT_TIMEOUT) -> T:
    """Poll ``probe`` until it returns something truthy, or fail saying what was awaited."""
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        last = probe()
        if last:
            return last
        time.sleep(POLL_INTERVAL)
    raise AssertionError(f"timed out after {timeout:.0f}s waiting for {what} (last saw {last!r})")


@pytest.fixture(scope="session")
def resources() -> Resources:
    try:
        loaded = Resources.load()
    except FileNotFoundError:
        pytest.skip("LocalStack is not provisioned - run `make local` first")
    try:
        with urllib.request.urlopen(
            f"{loaded.endpoint_url}/_localstack/health", timeout=5
        ) as response:
            json.loads(response.read())
    except (urllib.error.URLError, OSError, ValueError) as exc:
        pytest.skip(f"LocalStack is not reachable at {loaded.endpoint_url}: {exc}")
    return loaded


@pytest.fixture(scope="session")
def aws(resources: Resources) -> dict[str, Any]:
    """One client per service, built once for the session."""
    return {
        name: client(name, resources.endpoint_url, resources.region)
        for name in ("lambda", "sqs", "sns", "dynamodb", "logs", "apigateway")
    }


@pytest.fixture
def clean_tables(resources: Resources, aws: dict[str, Any]) -> Iterator[None]:
    """Empty the jobs and health tables before and after each test.

    Scenarios assert on counts and on first_seen, so they need a known-empty
    starting point - and leaving the table dirty would make the next test lie.
    """

    def purge() -> None:
        for table, key in (
            (resources.jobs_table, "job_id"),
            (resources.health_table, "scraper_key"),
        ):
            scanned = aws["dynamodb"].scan(TableName=table, ProjectionExpression=key)
            for item in scanned.get("Items", []):
                aws["dynamodb"].delete_item(TableName=table, Key={key: item[key]})
        # The Apply kit's table (pk/sk), when this provisioning has one.
        apply_table = getattr(resources, "apply_table", "")
        if apply_table:
            scanned = aws["dynamodb"].scan(TableName=apply_table, ProjectionExpression="pk, sk")
            for item in scanned.get("Items", []):
                aws["dynamodb"].delete_item(
                    TableName=apply_table, Key={"pk": item["pk"], "sk": item["sk"]}
                )

    purge()
    yield
    purge()


@pytest.fixture
def drain_queues(resources: Resources, aws: dict[str, Any]) -> Iterator[None]:
    """Purge the work queues and DLQs around a test."""

    def purge() -> None:
        for url in (
            resources.scrape_queue_url,
            resources.scrape_dlq_url,
            resources.notification_queue_url,
            resources.notification_dlq_url,
        ):
            if not url:
                continue
            # PurgeQueueInProgress (one purge per 60s) is harmless here.
            with contextlib.suppress(Exception):
                aws["sqs"].purge_queue(QueueUrl=url)

    purge()
    yield


def fixture_company(
    name: str = "TestCo",
    *,
    jobs: Sequence[dict[str, Any]] | None = None,
    fail: str | None = None,
    priority: Priority = Priority.HIGH,
) -> Company:
    """A registry entry backed by the deterministic `fixture` provider.

    It travels to the worker inside the ordinary ScrapeTask message, so the
    architecture test needs no entry in companies.json and no test-only code path
    in the deployed handler.
    """
    config: dict[str, Any] = {"fail": fail} if fail else {"jobs": list(jobs or [])}
    return Company(
        company=name,
        careers_url="https://example.test/careers",
        industry="Testing",
        priority=priority,
        provider="fixture",
        provider_config=config,
        support_status=SupportStatus.SUPPORTED,
        source_discovered_from=("aws_local",),
    )


def intern_posting(identifier: str = "1", **overrides: Any) -> dict[str, Any]:
    posting = {
        "title": "Software Engineer Intern",
        "url": f"https://example.test/jobs/{identifier}",
        "external_id": identifier,
        "location": "San Francisco, CA",
        "description": "Build distributed systems.",
        "employment_type": "Intern",
    }
    posting.update(overrides)
    return posting


def http_get_json(url: str, *, timeout: float = 30.0) -> tuple[int, Any]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            return response.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return exc.code, (json.loads(raw) if raw else None)
