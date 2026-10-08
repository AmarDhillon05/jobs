"""Table definitions, declared once.

The same key schema has to exist in four places: the CDK stack, the LocalStack
provisioner, the Moto-backed tests, and the queries in
:mod:`jobmonitor.storage.dynamo`. Declaring it here and having everything read
*this* means a renamed index breaks loudly in one place instead of drifting
between infrastructure and code. ``infrastructure/tests`` asserts the synthesized
CloudFormation matches these specs.
"""

from __future__ import annotations

import contextlib
from collections.abc import Sequence
from typing import Any

from jobmonitor.config import AwsSettings
from jobmonitor.storage.dynamo import COMPANY_INDEX, PENDING_INDEX, RECENT_INDEX

#: On-demand billing: a personal project polling every 10 minutes is nowhere near
#: the break-even point for provisioned capacity (see ARCHITECTURE.md costs).
BILLING_MODE = "PAY_PER_REQUEST"

JOBS_PARTITION_KEY = "job_id"
HEALTH_PARTITION_KEY = "scraper_key"
DEVICES_PARTITION_KEY = "device_id"

#: Attribute whose presence/absence makes ``pending-index`` sparse.
PENDING_KEY = "notification_pending"

#: TTL attribute on the health table, so diagnostics self-clean (PRD §10).
HEALTH_TTL_ATTRIBUTE = "expires_at"


def _string_attrs(*names: str) -> list[dict[str, str]]:
    return [{"AttributeName": name, "AttributeType": "S"} for name in names]


def jobs_table_spec(table_name: str) -> dict[str, Any]:
    """``create_table`` kwargs for the jobs table and its three GSIs."""
    return {
        "TableName": table_name,
        "BillingMode": BILLING_MODE,
        "KeySchema": [{"AttributeName": JOBS_PARTITION_KEY, "KeyType": "HASH"}],
        "AttributeDefinitions": _string_attrs(
            JOBS_PARTITION_KEY, "feed", "first_seen", "company", PENDING_KEY
        ),
        "GlobalSecondaryIndexes": [
            {
                # The feed: one query answers "what's new?".
                "IndexName": RECENT_INDEX,
                "KeySchema": [
                    {"AttributeName": "feed", "KeyType": "HASH"},
                    {"AttributeName": "first_seen", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": COMPANY_INDEX,
                "KeySchema": [
                    {"AttributeName": "company", "KeyType": "HASH"},
                    {"AttributeName": "first_seen", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                # Sparse: holds only jobs whose alert is still outstanding.
                "IndexName": PENDING_INDEX,
                "KeySchema": [
                    {"AttributeName": PENDING_KEY, "KeyType": "HASH"},
                    {"AttributeName": "first_seen", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
        ],
    }


def health_table_spec(table_name: str) -> dict[str, Any]:
    return {
        "TableName": table_name,
        "BillingMode": BILLING_MODE,
        "KeySchema": [{"AttributeName": HEALTH_PARTITION_KEY, "KeyType": "HASH"}],
        "AttributeDefinitions": _string_attrs(HEALTH_PARTITION_KEY),
    }


def devices_table_spec(table_name: str) -> dict[str, Any]:
    return {
        "TableName": table_name,
        "BillingMode": BILLING_MODE,
        "KeySchema": [{"AttributeName": DEVICES_PARTITION_KEY, "KeyType": "HASH"}],
        "AttributeDefinitions": _string_attrs(DEVICES_PARTITION_KEY),
    }


def all_specs(settings: AwsSettings) -> list[dict[str, Any]]:
    from jobmonitor.apply.store import apply_table_spec

    return [
        jobs_table_spec(settings.jobs_table),
        health_table_spec(settings.health_table),
        devices_table_spec(settings.devices_table),
        apply_table_spec(settings.apply_table),
    ]


def create_tables(resource: Any, settings: AwsSettings, *, wait: bool = True) -> list[Any]:
    """Create every table if absent. Safe to run repeatedly (local provisioning)."""
    from botocore.exceptions import ClientError

    tables = []
    for spec in all_specs(settings):
        try:
            table = resource.create_table(**spec)
            if wait:
                table.wait_until_exists()
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code not in {"ResourceInUseException", "TableAlreadyExistsException"}:
                raise
            table = resource.Table(spec["TableName"])
        tables.append(table)

    # TTL is a separate API call and is not supported by every emulator; a
    # failure here must not fail provisioning, since it only affects cleanup.
    with contextlib.suppress(Exception):
        resource.meta.client.update_time_to_live(
            TableName=settings.health_table,
            TimeToLiveSpecification={"Enabled": True, "AttributeName": HEALTH_TTL_ATTRIBUTE},
        )
    return tables


def delete_tables(resource: Any, settings: AwsSettings) -> None:
    """Drop every table. Test/teardown helper only."""
    from botocore.exceptions import ClientError

    for spec in all_specs(settings):
        try:
            resource.Table(spec["TableName"]).delete()
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
                raise


__all__: Sequence[str] = (
    "BILLING_MODE",
    "DEVICES_PARTITION_KEY",
    "HEALTH_PARTITION_KEY",
    "HEALTH_TTL_ATTRIBUTE",
    "JOBS_PARTITION_KEY",
    "PENDING_KEY",
    "all_specs",
    "create_tables",
    "delete_tables",
    "devices_table_spec",
    "health_table_spec",
    "jobs_table_spec",
)
