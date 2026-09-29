#!/usr/bin/env python3
"""Show the real AWS errors behind a failed deploy.

CloudFormation often reports a failed resource with a generic message
(``NotUpdatable``, "Internal Failure"). The underlying API call - Lambda's
CreateFunction, IAM's CreateRole, ... - recorded the specific reason in
CloudTrail's event history. This prints every failed call from the services
the stack touches, newest first.

    python scripts/diagnose_deploy.py                 # the last 2 hours
    python scripts/diagnose_deploy.py --hours 6 --all # every call, not just failures

Uses your normal AWS credentials; needs ``cloudtrail:LookupEvents``. CloudTrail
can lag up to ~15 minutes behind the failure, so run it again if it is empty.
Read-only: it creates and changes nothing.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Iterator, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

#: Services a JobMonitorStack deploy calls.
EVENT_SOURCES = (
    "lambda.amazonaws.com",
    "cloudformation.amazonaws.com",
    "iam.amazonaws.com",
    "dynamodb.amazonaws.com",
    "sqs.amazonaws.com",
    "sns.amazonaws.com",
    "events.amazonaws.com",
    "logs.amazonaws.com",
    "apigateway.amazonaws.com",
    "s3.amazonaws.com",
    "ssm.amazonaws.com",
)

#: Request fields worth showing; the rest (code locations, tags) is noise.
INTERESTING_PARAMETERS = (
    "functionName",
    "runtime",
    "memorySize",
    "timeout",
    "architectures",
    "reservedConcurrentExecutions",
    "roleName",
    "tableName",
    "queueName",
    "stackName",
    "logGroupName",
    "name",
)


def summarise(event: Mapping[str, Any], *, failures_only: bool = True) -> dict[str, Any] | None:
    """One CloudTrail record -> the fields that explain it, or None to skip it."""
    error_code = event.get("errorCode")
    if failures_only and not error_code:
        return None
    parameters = event.get("requestParameters") or {}
    shown = {
        key: parameters[key]
        for key in INTERESTING_PARAMETERS
        if isinstance(parameters, Mapping) and key in parameters
    }
    return {
        "time": event.get("eventTime"),
        "call": f"{str(event.get('eventSource', '')).split('.')[0]}:{event.get('eventName')}",
        "error": error_code,
        "message": event.get("errorMessage"),
        "request": shown,
        "caller": (event.get("userIdentity") or {}).get("arn"),
    }


def lookup(client: Any, source: str, start: datetime) -> Iterator[dict[str, Any]]:
    paginator = client.get_paginator("lookup_events")
    pages = paginator.paginate(
        LookupAttributes=[{"AttributeKey": "EventSource", "AttributeValue": source}],
        StartTime=start,
    )
    for page in pages:
        for item in page.get("Events", []):
            raw = item.get("CloudTrailEvent")
            if raw:
                yield json.loads(raw)


def collect(
    events: Iterable[Mapping[str, Any]], *, failures_only: bool = True
) -> list[dict[str, Any]]:
    rows = [row for event in events if (row := summarise(event, failures_only=failures_only))]
    return sorted(rows, key=lambda row: str(row["time"]), reverse=True)


def render(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return (
            "No failed calls found. CloudTrail can lag ~15 minutes behind a failure: "
            "run this again shortly, or widen the window with --hours."
        )
    lines = []
    for row in rows:
        lines.append(f"{row['time']}  {row['call']}  ->  {row['error'] or 'ok'}")
        if row["message"]:
            lines.append(f"    {row['message']}")
        if row["request"]:
            lines.append(f"    request: {json.dumps(row['request'], default=str)}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--hours", type=float, default=2.0, help="how far back to look")
    parser.add_argument("--all", action="store_true", help="show successful calls too")
    parser.add_argument("--region", help="defaults to your AWS profile's region")
    args = parser.parse_args(argv)

    import boto3
    from botocore.exceptions import BotoCoreError, ClientError

    client = boto3.client("cloudtrail", region_name=args.region)
    start = datetime.now(UTC) - timedelta(hours=args.hours)
    events: list[dict[str, Any]] = []
    try:
        for source in EVENT_SOURCES:
            events.extend(lookup(client, source, start))
    except (BotoCoreError, ClientError) as exc:
        print(f"could not read CloudTrail: {exc}", file=sys.stderr)
        return 1

    print(f"CloudTrail, last {args.hours:g}h, region {client.meta.region_name}:\n")
    print(render(collect(events, failures_only=not args.all)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
