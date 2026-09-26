"""PRD §13.2 - botocore Stubber: assert the exact AWS calls we make.

Moto proves the *behaviour* is right. These tests prove the *requests* are right,
which is a different question and catches a different class of bug: a query that
forgets its index name still "works" under a permissive mock by falling back to a
scan, and only shows up as a cost and latency problem in production.

A Stubber also fails on any call it was not programmed for, so these tests double
as proof that the repository makes no hidden extra requests and needs no network.
"""

from __future__ import annotations

from datetime import UTC, datetime

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.stub import ANY, Stubber

from jobmonitor.config import AwsSettings
from jobmonitor.models.job import Job
from jobmonitor.storage.dynamo import (
    COMPANY_INDEX,
    PENDING_INDEX,
    RECENT_INDEX,
    DynamoJobRepository,
)

pytestmark = pytest.mark.integration

SETTINGS = AwsSettings(region="us-east-1", jobs_table="stub-jobs")
NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)

JOB = Job(
    company="TestCo",
    title="Software Engineer Intern",
    url="https://job-boards.greenhouse.io/testco/jobs/1",
    source="greenhouse",
    external_id="1",
    location="San Francisco, CA",
)
JOB_ID = "testco:1"


@pytest.fixture
def stubbed() -> tuple[DynamoJobRepository, Stubber]:
    resource = boto3.resource("dynamodb", region_name=SETTINGS.region)
    stubber = Stubber(resource.meta.client)
    stubber.activate()
    repository = DynamoJobRepository(SETTINGS, resource=resource)
    return repository, stubber


class TestUpsertRequestShape:
    def test_a_new_job_issues_one_update_item_then_marks_it_pending(
        self, stubbed: tuple[DynamoJobRepository, Stubber]
    ) -> None:
        repository, stubber = stubbed
        stubber.add_response(
            "update_item",
            {},  # no Attributes => the item did not exist
            {
                "TableName": "stub-jobs",
                "Key": {"job_id": JOB_ID},
                "UpdateExpression": ANY,
                "ExpressionAttributeNames": ANY,
                "ExpressionAttributeValues": ANY,
                "ReturnValues": "ALL_OLD",
            },
        )
        stubber.add_response(
            "update_item",
            {},
            {
                "TableName": "stub-jobs",
                "Key": {"job_id": JOB_ID},
                "UpdateExpression": "SET notification_pending = :pending",
                "ConditionExpression": "notification_sent = :false",
                "ExpressionAttributeValues": {":pending": "pending", ":false": False},
            },
        )
        result = repository.upsert(JOB, relevance_score=73, now=NOW)
        assert result.is_new is True
        stubber.assert_no_pending_responses()

    def test_the_update_expression_protects_first_seen_and_notification_state(
        self, stubbed: tuple[DynamoJobRepository, Stubber]
    ) -> None:
        repository, stubber = stubbed
        captured: dict[str, object] = {}

        stubber.add_response("update_item", {"Attributes": {"job_id": {"S": JOB_ID}}}, None)
        original = repository.table.meta.client.update_item

        def spy(**kwargs: object) -> object:
            captured.update(kwargs)
            return original(**kwargs)

        repository.table.meta.client.update_item = spy  # type: ignore[method-assign]
        repository.upsert(JOB, now=NOW)

        expression = str(captured["UpdateExpression"])
        # These three clauses are the whole dedup guarantee.
        assert "first_seen = if_not_exists(first_seen, :first_seen)" in expression
        assert "last_seen = :last_seen" in expression
        assert "notification_sent = if_not_exists(notification_sent, :false)" in expression
        assert "times_seen = if_not_exists(times_seen, :zero) + :one" in expression
        # And this must NOT be there: it would resurrect notified jobs.
        assert "if_not_exists(notification_pending" not in expression

    def test_an_existing_job_makes_exactly_one_request(
        self, stubbed: tuple[DynamoJobRepository, Stubber]
    ) -> None:
        repository, stubber = stubbed
        stubber.add_response(
            "update_item",
            {
                "Attributes": {
                    "job_id": {"S": JOB_ID},
                    "first_seen": {"S": NOW.isoformat()},
                    "content_hash": {"S": "same"},
                    "times_seen": {"N": "3"},
                    "notification_sent": {"BOOL": True},
                }
            },
            None,
        )
        result = repository.upsert(JOB, now=NOW)
        # No second call: the steady-state path is a single write.
        assert result.is_new is False
        stubber.assert_no_pending_responses()

    def test_every_refreshed_attribute_is_escaped_via_attribute_names(
        self, stubbed: tuple[DynamoJobRepository, Stubber]
    ) -> None:
        """`source` and friends are DynamoDB reserved words."""
        repository, stubber = stubbed
        captured: dict[str, object] = {}
        stubber.add_response("update_item", {"Attributes": {"job_id": {"S": JOB_ID}}}, None)
        original = repository.table.meta.client.update_item

        def spy(**kwargs: object) -> object:
            captured.update(kwargs)
            return original(**kwargs)

        repository.table.meta.client.update_item = spy  # type: ignore[method-assign]
        repository.upsert(JOB, now=NOW)

        names = captured["ExpressionAttributeNames"]
        assert isinstance(names, dict)
        assert "#source" in names and names["#source"] == "source"
        expression = str(captured["UpdateExpression"])
        assert " source = " not in expression  # never unescaped


class TestQueryRequestShape:
    def test_recent_queries_the_recent_index_newest_first(
        self, stubbed: tuple[DynamoJobRepository, Stubber]
    ) -> None:
        repository, stubber = stubbed
        stubber.add_response(
            "query",
            {"Items": []},
            {
                "TableName": "stub-jobs",
                "IndexName": RECENT_INDEX,
                "KeyConditionExpression": ANY,
                "ScanIndexForward": False,
                "Limit": 25,
            },
        )
        assert repository.recent(limit=25) == []
        stubber.assert_no_pending_responses()

    def test_by_company_queries_the_company_index(
        self, stubbed: tuple[DynamoJobRepository, Stubber]
    ) -> None:
        repository, stubber = stubbed
        stubber.add_response(
            "query",
            {"Items": []},
            {
                "TableName": "stub-jobs",
                "IndexName": COMPANY_INDEX,
                "KeyConditionExpression": ANY,
                "ScanIndexForward": False,
                "Limit": 50,
            },
        )
        assert repository.by_company("TestCo") == []
        stubber.assert_no_pending_responses()

    def test_pending_queries_the_sparse_index_oldest_first(
        self, stubbed: tuple[DynamoJobRepository, Stubber]
    ) -> None:
        repository, stubber = stubbed
        stubber.add_response(
            "query",
            {"Items": []},
            {
                "TableName": "stub-jobs",
                "IndexName": PENDING_INDEX,
                "KeyConditionExpression": ANY,
                "ScanIndexForward": True,
                "Limit": 100,
            },
        )
        assert repository.pending_notifications() == []
        stubber.assert_no_pending_responses()

    def test_get_uses_get_item_not_a_scan(
        self, stubbed: tuple[DynamoJobRepository, Stubber]
    ) -> None:
        repository, stubber = stubbed
        stubber.add_response("get_item", {}, {"TableName": "stub-jobs", "Key": {"job_id": JOB_ID}})
        assert repository.get(JOB_ID) is None
        stubber.assert_no_pending_responses()


class TestMarkNotifiedRequestShape:
    def test_sends_both_guards_and_removes_the_pending_marker(
        self, stubbed: tuple[DynamoJobRepository, Stubber]
    ) -> None:
        repository, stubber = stubbed
        stubber.add_response(
            "update_item",
            {},
            {
                "TableName": "stub-jobs",
                "Key": {"job_id": JOB_ID},
                "UpdateExpression": (
                    "SET notification_sent = :true, notified_at = :at REMOVE notification_pending"
                ),
                "ConditionExpression": ("attribute_exists(job_id) AND notification_sent = :false"),
                "ExpressionAttributeValues": {
                    ":true": True,
                    ":false": False,
                    ":at": NOW.isoformat(),
                },
            },
        )
        assert repository.mark_notified([JOB_ID], now=NOW) == 1
        stubber.assert_no_pending_responses()

    def test_a_failed_condition_is_swallowed_as_already_notified(
        self, stubbed: tuple[DynamoJobRepository, Stubber]
    ) -> None:
        repository, stubber = stubbed
        stubber.add_client_error(
            "update_item",
            service_error_code="ConditionalCheckFailedException",
            service_message="The conditional request failed",
            http_status_code=400,
        )
        # Not an error: it means someone already sent the alert.
        assert repository.mark_notified([JOB_ID], now=NOW) == 0

    def test_a_throttling_error_is_not_swallowed(
        self, stubbed: tuple[DynamoJobRepository, Stubber]
    ) -> None:
        repository, stubber = stubbed
        stubber.add_client_error(
            "update_item",
            service_error_code="ProvisionedThroughputExceededException",
            service_message="Rate exceeded",
            http_status_code=400,
        )
        with pytest.raises(ClientError) as excinfo:
            repository.mark_notified([JOB_ID], now=NOW)
        assert excinfo.value.response["Error"]["Code"] == ("ProvisionedThroughputExceededException")

    def test_a_server_error_is_not_swallowed(
        self, stubbed: tuple[DynamoJobRepository, Stubber]
    ) -> None:
        repository, stubber = stubbed
        stubber.add_client_error(
            "update_item",
            service_error_code="InternalServerError",
            service_message="boom",
            http_status_code=500,
        )
        with pytest.raises(ClientError):
            repository.mark_notified([JOB_ID], now=NOW)

    def test_one_failure_does_not_stop_the_rest_of_the_batch(
        self, stubbed: tuple[DynamoJobRepository, Stubber]
    ) -> None:
        repository, stubber = stubbed
        stubber.add_client_error(
            "update_item",
            service_error_code="ConditionalCheckFailedException",
            service_message="already sent",
            http_status_code=400,
        )
        stubber.add_response("update_item", {}, None)
        assert repository.mark_notified(["already:1", "fresh:2"], now=NOW) == 1


class TestEndpointConfiguration:
    def test_endpoint_url_is_honoured_for_localstack(self) -> None:
        settings = AwsSettings(region="us-east-1", endpoint_url="http://localhost:4566")
        repository = DynamoJobRepository(settings)
        assert repository.table.meta.client.meta.endpoint_url == "http://localhost:4566"

    def test_no_endpoint_url_uses_the_real_regional_endpoint(self) -> None:
        repository = DynamoJobRepository(AwsSettings(region="eu-west-2"))
        endpoint = repository.table.meta.client.meta.endpoint_url
        assert "eu-west-2" in endpoint
        assert "localhost" not in endpoint
