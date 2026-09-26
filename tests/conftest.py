"""Shared fixtures.

Two global safety rails live here:

* AWS credentials and region are forced to fake values for every test, so no test
  can accidentally reach a real AWS account (PRD §12) even though the sandbox has
  ambient credentials in its environment.
* ``AWS_ENDPOINT_URL`` is cleared unless a test opts in, so a stray boto3 client
  cannot silently talk to something real.
"""

from __future__ import annotations

import os
import random
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.support.http import FakeTransport, RecordingSleeper, ScriptedResponse  # noqa: E402

from jobmonitor.config import Settings, for_tests  # noqa: E402
from jobmonitor.http import HttpClient  # noqa: E402
from jobmonitor.models.company import Company, Priority, SupportStatus  # noqa: E402

_FAKE_AWS_ENV = {
    "AWS_ACCESS_KEY_ID": "testing",
    "AWS_SECRET_ACCESS_KEY": "testing",
    "AWS_SESSION_TOKEN": "testing",
    "AWS_SECURITY_TOKEN": "testing",
    "AWS_DEFAULT_REGION": "us-east-1",
    "AWS_REGION": "us-east-1",
}


@pytest.fixture(autouse=True)
def _isolate_aws_environment(request: pytest.FixtureRequest) -> Iterator[None]:
    """Never let a test see real AWS credentials or a real endpoint."""
    previous = {key: os.environ.get(key) for key in (*_FAKE_AWS_ENV, "AWS_ENDPOINT_URL")}
    os.environ.update(_FAKE_AWS_ENV)
    # aws_local tests talk to LocalStack and set their own endpoint.
    if "aws_local" not in request.keywords:
        os.environ.pop("AWS_ENDPOINT_URL", None)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.fixture
def settings() -> Settings:
    return for_tests()


@pytest.fixture
def sleeper() -> RecordingSleeper:
    return RecordingSleeper()


@pytest.fixture
def deterministic_rng() -> random.Random:
    return random.Random(1234)


@pytest.fixture
def client_factory(settings: Settings, sleeper: RecordingSleeper, deterministic_rng: random.Random):
    """Build an :class:`HttpClient` around a scripted transport, with no waiting."""

    def factory(transport: FakeTransport, **overrides: object) -> HttpClient:
        http_settings = settings.http
        if overrides:
            from dataclasses import replace

            http_settings = replace(http_settings, **overrides)  # type: ignore[arg-type]
        return HttpClient(http_settings, transport=transport, sleep=sleeper, rng=deterministic_rng)

    return factory


def make_company(
    name: str = "TestCo",
    provider: str = "greenhouse",
    config: dict[str, object] | None = None,
    **kwargs: object,
) -> Company:
    """A registry entry for tests, with sensible per-provider defaults."""
    defaults: dict[str, dict[str, object]] = {
        "greenhouse": {"board_token": "testco"},
        "lever": {"site": "testco"},
        "ashby": {"job_board_name": "testco"},
        "smartrecruiters": {"company_id": "TestCo"},
        "workable": {"subdomain": "testco"},
        "rippling": {"board_slug": "testco"},
        "workday": {
            "tenant": "testco",
            "host": "testco.wd1.myworkdayjobs.com",
            "site": "External",
        },
    }
    payload: dict[str, object] = {
        "company": name,
        "careers_url": "https://example.test/careers",
        "industry": "Testing",
        "priority": Priority.HIGH,
        "provider": provider,
        "provider_config": config if config is not None else defaults.get(provider, {}),
        "support_status": SupportStatus.SUPPORTED,
        "source_discovered_from": ("test",),
    }
    payload.update(kwargs)
    return Company(**payload)  # type: ignore[arg-type]


@pytest.fixture
def company_factory():
    return make_company


@pytest.fixture
def ok_response() -> ScriptedResponse:
    return ScriptedResponse.json({})
