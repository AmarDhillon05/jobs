"""scripts/diagnose_deploy.py - turning CloudTrail records into the reason a deploy failed."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import diagnose_deploy

pytestmark = pytest.mark.unit

FAILED_CREATE = {
    "eventTime": "2026-09-29T01:57:50Z",
    "eventSource": "lambda.amazonaws.com",
    "eventName": "CreateFunction20150331",
    "errorCode": "InvalidParameterValueException",
    "errorMessage": "Specified ReservedConcurrentExecutions for function decreases "
    "account's UnreservedConcurrentExecution below its minimum value of [10].",
    "requestParameters": {
        "functionName": "JobMonitorStack-WorkerFunction",
        "memorySize": 512,
        "code": {"s3Bucket": "cdk-assets"},
    },
    "userIdentity": {"arn": "arn:aws:iam::123456789012:user/me"},
}
OK_CALL = {**FAILED_CREATE, "eventTime": "2026-09-29T01:57:40Z", "errorCode": None}


def test_a_failed_call_is_summarised_with_its_real_message() -> None:
    row = diagnose_deploy.summarise(FAILED_CREATE)
    assert row is not None
    assert row["call"] == "lambda:CreateFunction20150331"
    assert row["error"] == "InvalidParameterValueException"
    assert "UnreservedConcurrentExecution" in row["message"]
    # Only the fields that explain the call, not code locations.
    assert row["request"] == {"functionName": "JobMonitorStack-WorkerFunction", "memorySize": 512}


def test_successful_calls_are_skipped_unless_asked_for() -> None:
    assert diagnose_deploy.summarise(OK_CALL) is None
    assert diagnose_deploy.summarise(OK_CALL, failures_only=False) is not None


def test_newest_failure_first_and_rendered_readably() -> None:
    older = {**FAILED_CREATE, "eventTime": "2026-09-29T01:00:00Z", "errorMessage": "older"}
    rows = diagnose_deploy.collect([older, FAILED_CREATE, OK_CALL])
    assert [row["message"] for row in rows][1] == "older"
    text = diagnose_deploy.render(rows)
    assert text.splitlines()[0].endswith(
        "lambda:CreateFunction20150331  ->  InvalidParameterValueException"
    )


def test_nothing_found_says_why_it_might_be_empty() -> None:
    assert "15 minutes" in diagnose_deploy.render([])


def test_reads_every_page_of_cloudtrail() -> None:
    import json
    from datetime import UTC, datetime

    class Paginator:
        def paginate(self, **kwargs: object):  # type: ignore[no-untyped-def]
            assert kwargs["LookupAttributes"] == [
                {"AttributeKey": "EventSource", "AttributeValue": "lambda.amazonaws.com"}
            ]
            yield {"Events": [{"CloudTrailEvent": json.dumps(FAILED_CREATE)}]}
            yield {"Events": [{"CloudTrailEvent": json.dumps(OK_CALL)}, {}]}

    class Client:
        def get_paginator(self, name: str) -> Paginator:
            assert name == "lookup_events"
            return Paginator()

    events = list(diagnose_deploy.lookup(Client(), "lambda.amazonaws.com", datetime.now(UTC)))
    assert len(events) == 2
