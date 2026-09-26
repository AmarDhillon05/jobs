"""Persistence (PRD §10).

Two interchangeable implementations behind one set of interfaces:

* :class:`InMemoryJobRepository` - tests, local dev, the local API server.
* :class:`DynamoJobRepository` - the deployable one, exercised under Moto
  (unit level), botocore stubs (request shape) and LocalStack (system level).

Both are held to the same contract by ``tests/integration/test_repository_contract.py``,
so a scenario that passes in memory and fails on DynamoDB isolates the fault to
the AWS layer rather than the pipeline.
"""

from jobmonitor.storage.base import DeviceRepository, HealthRepository, JobRepository
from jobmonitor.storage.dynamo import (
    COMPANY_INDEX,
    PENDING_INDEX,
    RECENT_INDEX,
    DynamoDeviceRepository,
    DynamoHealthRepository,
    DynamoJobRepository,
    build_resource,
)
from jobmonitor.storage.memory import (
    InMemoryDeviceRepository,
    InMemoryHealthRepository,
    InMemoryJobRepository,
)

__all__ = [
    "COMPANY_INDEX",
    "PENDING_INDEX",
    "RECENT_INDEX",
    "DeviceRepository",
    "DynamoDeviceRepository",
    "DynamoHealthRepository",
    "DynamoJobRepository",
    "HealthRepository",
    "InMemoryDeviceRepository",
    "InMemoryHealthRepository",
    "InMemoryJobRepository",
    "JobRepository",
    "build_resource",
]
