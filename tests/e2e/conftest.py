"""Fixtures for Level 8 - the PRD §30 acceptance scenarios.

The whole system is assembled here from its real parts: the real filter, the real
identity/fingerprint logic, the real pipeline, the real notifier, the real API
router. Only two things are substituted, and only at the edges:

* the **network**, by a scripted transport or the deterministic ``fixture``
  provider - PRD §14 permits this explicitly, and it is what makes "a 500 then a
  500 then a 200" an assertion rather than a hope;
* the **delivery transports**, by in-memory sinks - PRD §22 asks for exactly that.

Storage is parametrized over the in-memory store *and* DynamoDB under Moto, so
every scenario is proven twice: once fast, once against real DynamoDB semantics.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest
from tests.conftest import make_company

from jobmonitor.api.routes import Api, Request
from jobmonitor.config import (
    AwsSettings,
    EmailSettings,
    FilterSettings,
    PushSettings,
    Settings,
    for_tests,
)
from jobmonitor.filtering import JobFilter
from jobmonitor.models.company import Company
from jobmonitor.models.record import DeviceRegistration
from jobmonitor.notifications import Notifier
from jobmonitor.notifications.transports import MemoryEmailTransport, MemoryPushTransport
from jobmonitor.orchestration import PollRunner
from jobmonitor.scrapers.base import build_source
from jobmonitor.storage.base import HealthRepository, JobRepository
from jobmonitor.storage.dynamo import DynamoHealthRepository, DynamoJobRepository
from jobmonitor.storage.memory import (
    InMemoryDeviceRepository,
    InMemoryHealthRepository,
    InMemoryJobRepository,
)
from jobmonitor.storage.tables import create_tables

T0 = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
APP_BASE = "https://app.test"
AWS_TEST_SETTINGS = AwsSettings(
    region="us-east-1",
    jobs_table="e2e-jobs",
    health_table="e2e-health",
    devices_table="e2e-devices",
)


def fixture_company(
    name: str = "TestCo",
    *,
    jobs: Sequence[dict[str, Any]] | None = None,
    fail: str | None = None,
    **kwargs: Any,
) -> Company:
    """A registry entry whose postings are configured inline."""
    config: dict[str, Any] = {"fail": fail} if fail else {"jobs": list(jobs or [])}
    return make_company(name, provider="fixture", config=config, **kwargs)


def posting(identifier: str = "1", **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "title": "Software Engineer Intern",
        "url": f"https://boards.testco.test/jobs/{identifier}?gh_src=feed",
        "external_id": identifier,
        "location": "San Francisco, CA",
        "description": "Build the thing.",
        "date_posted": "2026-09-25T00:00:00Z",
    }
    payload.update(overrides)
    return payload


@dataclass
class System:
    """The assembled system, plus the seams a scenario needs to poke."""

    settings: Settings
    repository: JobRepository
    health: HealthRepository
    devices: InMemoryDeviceRepository
    email: MemoryEmailTransport
    push: MemoryPushTransport
    notifier: Notifier
    runner: PollRunner
    api: Api
    storage_label: str
    #: Per-company source overrides, so one company can be a scripted HTTP source.
    sources: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------ polling
    def poll(
        self, companies: Sequence[Company], *, at: datetime | None = None, poll_id: str = "poll"
    ):
        return self.runner.run(companies, poll_id=poll_id, now=at or T0)

    # ---------------------------------------------------------------------- API
    def get(self, path: str, **params: str) -> tuple[int, Any]:
        """Call the real API router the client talks to."""
        response = self.api.handle(Request(method="GET", path=path, query=dict(params)))
        # Response.body is already a Python object; the Lambda adapter serialises it.
        return response.status, response.body

    def feed(self, limit: int = 50) -> list[dict[str, Any]]:
        status, payload = self.get("/jobs", limit=str(limit))
        assert status == 200, payload
        return list(payload["jobs"])

    def register_phone(self, device_id: str = "ios-phone") -> DeviceRegistration:
        device = DeviceRegistration(
            device_id=device_id, token=f"ExponentPushToken[{device_id}]", transport="expo"
        )
        return self.devices.register(device)

    # ------------------------------------------------------------- convenience
    @property
    def push_payloads(self) -> list[dict[str, str]]:
        return [message.data for message, _devices in self.push.sent]

    def stored(self) -> list[Any]:
        return self.repository.recent(limit=100)


def build_system(
    storage: str,
    *,
    resource: Any = None,
    filters: FilterSettings | None = None,
) -> System:
    settings = for_tests(
        aws=AWS_TEST_SETTINGS,
        email=EmailSettings(transport="memory", recipient="you@example.test"),
        push=PushSettings(transport="memory"),
        app_base_url=APP_BASE,
        filters=filters or FilterSettings(),
    )
    if storage == "memory":
        repository: JobRepository = InMemoryJobRepository()
        health: HealthRepository = InMemoryHealthRepository()
    else:
        repository = DynamoJobRepository(AWS_TEST_SETTINGS, resource=resource)
        health = DynamoHealthRepository(AWS_TEST_SETTINGS, resource=resource)

    devices = InMemoryDeviceRepository()
    email = MemoryEmailTransport()
    push = MemoryPushTransport()
    notifier = Notifier(
        settings,
        repository=repository,
        email_transport=email,
        push_transport=push,
        device_repository=devices,
    )
    system = System(
        settings=settings,
        repository=repository,
        health=health,
        devices=devices,
        email=email,
        push=push,
        notifier=notifier,
        runner=PollRunner(
            settings,
            repository=repository,
            notifier=notifier,
            health_repository=health,
            job_filter=JobFilter(settings.filters),
        ),
        api=Api(repository=repository, device_repository=devices, write_token="test-token"),
        storage_label=storage,
    )

    def source_for(company: Company) -> Any:
        override = system.sources.get(company.company)
        return override if override is not None else build_source(company, None)

    system.runner.source_factory = source_for
    return system


@pytest.fixture(params=["memory", "dynamodb-moto"])
def system(request: pytest.FixtureRequest) -> Iterator[System]:
    """The whole system, once per storage implementation."""
    if request.param == "memory":
        yield build_system("memory")
        return

    import boto3
    from moto import mock_aws

    with mock_aws():
        resource = boto3.resource("dynamodb", region_name=AWS_TEST_SETTINGS.region)
        create_tables(resource, AWS_TEST_SETTINGS, wait=False)
        yield build_system("dynamodb-moto", resource=resource)


@pytest.fixture
def memory_system() -> System:
    """The fast single-storage system, for scenarios storage cannot influence."""
    return build_system("memory")
