"""Fixtures for Level 3/4/6 tests.

``repository`` is parametrized over both implementations, so every persistence
assertion runs against the in-memory store *and* against real DynamoDB semantics
under Moto. That is what makes the in-memory store trustworthy as the fast path:
it is held to the same contract as the thing that ships.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from jobmonitor.config import AwsSettings
from jobmonitor.storage.base import DeviceRepository, HealthRepository, JobRepository
from jobmonitor.storage.dynamo import (
    DynamoDeviceRepository,
    DynamoHealthRepository,
    DynamoJobRepository,
)
from jobmonitor.storage.memory import (
    InMemoryDeviceRepository,
    InMemoryHealthRepository,
    InMemoryJobRepository,
)
from jobmonitor.storage.tables import create_tables

AWS_TEST_SETTINGS = AwsSettings(
    region="us-east-1",
    jobs_table="test-jobs",
    health_table="test-health",
    devices_table="test-devices",
)


@pytest.fixture
def aws_settings() -> AwsSettings:
    return AWS_TEST_SETTINGS


@pytest.fixture
def moto_dynamodb() -> Iterator[object]:
    """A Moto-backed DynamoDB with this project's real table definitions."""
    import boto3
    from moto import mock_aws

    with mock_aws():
        resource = boto3.resource("dynamodb", region_name=AWS_TEST_SETTINGS.region)
        create_tables(resource, AWS_TEST_SETTINGS, wait=False)
        yield resource


@pytest.fixture(params=["memory", "dynamodb-moto"])
def repository(request: pytest.FixtureRequest) -> Iterator[JobRepository]:
    if request.param == "memory":
        yield InMemoryJobRepository()
        return
    resource = request.getfixturevalue("moto_dynamodb")
    yield DynamoJobRepository(AWS_TEST_SETTINGS, resource=resource)


@pytest.fixture(params=["memory", "dynamodb-moto"])
def health_repository(request: pytest.FixtureRequest) -> Iterator[HealthRepository]:
    if request.param == "memory":
        yield InMemoryHealthRepository()
        return
    resource = request.getfixturevalue("moto_dynamodb")
    yield DynamoHealthRepository(AWS_TEST_SETTINGS, resource=resource)


@pytest.fixture(params=["memory", "dynamodb-moto"])
def device_repository(request: pytest.FixtureRequest) -> Iterator[DeviceRepository]:
    if request.param == "memory":
        yield InMemoryDeviceRepository()
        return
    resource = request.getfixturevalue("moto_dynamodb")
    yield DynamoDeviceRepository(AWS_TEST_SETTINGS, resource=resource)


@pytest.fixture
def memory_repository() -> InMemoryJobRepository:
    return InMemoryJobRepository()
