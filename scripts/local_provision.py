#!/usr/bin/env python3
"""Provision the whole architecture inside LocalStack.

Creates the same resource *types* the CDK stack deploys - DynamoDB tables with
their GSIs, SQS queues with redrive policies, an SNS topic fanning out to a queue,
four Lambda functions from the real deployment bundle, SQS event source mappings
with partial-batch responses, and an HTTP API in front of the API Lambda - so the
Level-7 test exercises genuine AWS APIs rather than an in-process stand-in.

Why boto3 rather than ``cdklocal deploy``: it provisions in a few seconds instead
of minutes, needs no CloudFormation bootstrap, and fails with an error that names
the resource. Table key schemas come from ``jobmonitor.storage.tables``, which the
CDK template is asserted against, so the two cannot silently diverge.
``ARCHITECTURE.md`` records exactly which boundaries this simulates rather than
natively exercises.

    python scripts/local_provision.py                 # create everything
    python scripts/local_provision.py --teardown      # remove everything
    python scripts/local_provision.py --print         # show resource ids
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import time
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from build_lambda_bundle import build as build_lambda_bundle  # noqa: E402
from jobmonitor.config import AwsSettings  # noqa: E402
from jobmonitor.storage.tables import create_tables, delete_tables  # noqa: E402

RESOURCES_PATH = REPO_ROOT / ".localstack" / "resources.json"

JOBS_TABLE = "jobmonitor-jobs"
HEALTH_TABLE = "jobmonitor-scraper-health"
DEVICES_TABLE = "jobmonitor-devices"
SCRAPE_QUEUE = "jobmonitor-scrape"
SCRAPE_DLQ = "jobmonitor-scrape-dlq"
NOTIFICATION_QUEUE = "jobmonitor-notify"
NOTIFICATION_DLQ = "jobmonitor-notify-dlq"
NOTIFICATION_TOPIC = "jobmonitor-new-jobs"
LAMBDA_ROLE_NAME = "jobmonitor-lambda-role"
API_NAME = "jobmonitor-api"
API_STAGE = "local"
API_STAGE = "local"

MAX_RECEIVE_COUNT = 3
#: Shorter than the deployed stack's 5 minutes. Local shards use deterministic
#: fixtures and finish in seconds, and the deployed value would make a poison
#: message take 3 x 240s = 12 minutes to reach the DLQ, which is dead time in
#: every test run. Still comfortably longer than any local invocation.
WORKER_TIMEOUT = 55
#: Must exceed the function timeout or SQS redelivers a shard that is still
#: running - the one invariant that must hold locally as well as deployed.
VISIBILITY_TIMEOUT = 60

FUNCTIONS: tuple[tuple[str, str], ...] = (
    ("jobmonitor-coordinator", "jobmonitor.orchestration.handlers.coordinator_handler"),
    ("jobmonitor-worker", "jobmonitor.orchestration.handlers.worker_handler"),
    ("jobmonitor-notifier", "jobmonitor.orchestration.handlers.notifier_handler"),
    ("jobmonitor-api", "jobmonitor.api.lambda_handler.handler"),
)

TRUST_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {"Service": "lambda.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }
    ],
}


@dataclass
class Resources:
    """Everything the tests and the CLI need to talk to the local stack."""

    endpoint_url: str
    region: str = "us-east-1"
    jobs_table: str = JOBS_TABLE
    health_table: str = HEALTH_TABLE
    devices_table: str = DEVICES_TABLE
    scrape_queue_url: str = ""
    scrape_dlq_url: str = ""
    notification_queue_url: str = ""
    notification_dlq_url: str = ""
    notification_topic_arn: str = ""
    lambda_role_arn: str = ""
    functions: dict[str, str] = field(default_factory=dict)
    api_id: str = ""
    api_url: str = ""

    def to_settings(self) -> AwsSettings:
        return AwsSettings(
            region=self.region,
            endpoint_url=self.endpoint_url,
            jobs_table=self.jobs_table,
            health_table=self.health_table,
            devices_table=self.devices_table,
            scrape_queue_url=self.scrape_queue_url or None,
            scrape_dlq_url=self.scrape_dlq_url or None,
            notification_topic_arn=self.notification_topic_arn or None,
            notification_queue_url=self.notification_queue_url or None,
            notification_dlq_url=self.notification_dlq_url or None,
        )

    def save(self, path: Path = RESOURCES_PATH) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: Path = RESOURCES_PATH) -> Resources:
        if not path.exists():
            raise FileNotFoundError(
                f"{path} is missing - run `make local-provision` (or `make local`) first"
            )
        return cls(**json.loads(path.read_text(encoding="utf-8")))


def client(service: str, endpoint_url: str, region: str = "us-east-1") -> Any:
    import boto3

    return boto3.client(
        service,
        region_name=region,
        endpoint_url=endpoint_url,
        # LocalStack accepts any credentials; being explicit keeps the ambient
        # environment (which may hold real ones) out of the picture entirely.
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )


def bundle_zip() -> bytes:
    """Zip the real deployment bundle in memory."""
    bundle = build_lambda_bundle()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(bundle.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(bundle).as_posix())
    return buffer.getvalue()


# --------------------------------------------------------------------- creation


def provision_tables(resources: Resources) -> None:
    import boto3

    dynamodb = boto3.resource(
        "dynamodb",
        region_name=resources.region,
        endpoint_url=resources.endpoint_url,
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    create_tables(dynamodb, resources.to_settings(), wait=True)
    print(f"  tables: {resources.jobs_table}, {resources.health_table}, {resources.devices_table}")


def provision_queues(resources: Resources) -> None:
    sqs = client("sqs", resources.endpoint_url, resources.region)

    def ensure(name: str, attributes: dict[str, str] | None = None) -> str:
        try:
            return str(sqs.create_queue(QueueName=name, Attributes=attributes or {})["QueueUrl"])
        except sqs.exceptions.QueueNameExists:
            url = str(sqs.get_queue_url(QueueName=name)["QueueUrl"])
            if attributes:
                sqs.set_queue_attributes(QueueUrl=url, Attributes=attributes)
            return url

    def arn_of(queue_url: str) -> str:
        return str(
            sqs.get_queue_attributes(QueueUrl=queue_url, AttributeNames=["QueueArn"])["Attributes"][
                "QueueArn"
            ]
        )

    resources.scrape_dlq_url = ensure(SCRAPE_DLQ, {"MessageRetentionPeriod": "1209600"})
    resources.scrape_queue_url = ensure(
        SCRAPE_QUEUE,
        {
            "VisibilityTimeout": str(VISIBILITY_TIMEOUT),
            "RedrivePolicy": json.dumps(
                {
                    "deadLetterTargetArn": arn_of(resources.scrape_dlq_url),
                    "maxReceiveCount": str(MAX_RECEIVE_COUNT),
                }
            ),
        },
    )
    resources.notification_dlq_url = ensure(NOTIFICATION_DLQ, {"MessageRetentionPeriod": "1209600"})
    resources.notification_queue_url = ensure(
        NOTIFICATION_QUEUE,
        {
            "VisibilityTimeout": "120",
            "RedrivePolicy": json.dumps(
                {
                    "deadLetterTargetArn": arn_of(resources.notification_dlq_url),
                    "maxReceiveCount": str(MAX_RECEIVE_COUNT),
                }
            ),
        },
    )
    print(f"  queues: {SCRAPE_QUEUE} (+dlq), {NOTIFICATION_QUEUE} (+dlq)")


def provision_topic(resources: Resources) -> None:
    sns = client("sns", resources.endpoint_url, resources.region)
    sqs = client("sqs", resources.endpoint_url, resources.region)

    resources.notification_topic_arn = str(sns.create_topic(Name=NOTIFICATION_TOPIC)["TopicArn"])
    queue_arn = sqs.get_queue_attributes(
        QueueUrl=resources.notification_queue_url, AttributeNames=["QueueArn"]
    )["Attributes"]["QueueArn"]

    existing = sns.list_subscriptions_by_topic(TopicArn=resources.notification_topic_arn)
    if not any(
        subscription.get("Endpoint") == queue_arn
        for subscription in existing.get("Subscriptions", [])
    ):
        sns.subscribe(TopicArn=resources.notification_topic_arn, Protocol="sqs", Endpoint=queue_arn)
    print(f"  topic: {NOTIFICATION_TOPIC} -> {NOTIFICATION_QUEUE}")


def provision_role(resources: Resources) -> None:
    iam = client("iam", resources.endpoint_url, resources.region)
    try:
        role = iam.create_role(
            RoleName=LAMBDA_ROLE_NAME, AssumeRolePolicyDocument=json.dumps(TRUST_POLICY)
        )
        resources.lambda_role_arn = str(role["Role"]["Arn"])
    except iam.exceptions.EntityAlreadyExistsException:
        resources.lambda_role_arn = str(iam.get_role(RoleName=LAMBDA_ROLE_NAME)["Role"]["Arn"])

    # LocalStack does not enforce IAM by default, so this role is a placeholder
    # for the deployed least-privilege policies. The real ones are asserted in
    # infrastructure/tests/test_stack.py.
    iam.put_role_policy(
        RoleName=LAMBDA_ROLE_NAME,
        PolicyName="jobmonitor-local",
        PolicyDocument=json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": ["dynamodb:*", "sqs:*", "sns:*", "logs:*", "ses:*"],
                        "Resource": "*",
                    }
                ],
            }
        ),
    )
    print(f"  role: {LAMBDA_ROLE_NAME}")


def function_environment(resources: Resources) -> dict[str, str]:
    return {
        "AWS_ENDPOINT_URL": "http://localhost.localstack.cloud:4566",
        "AWS_REGION": resources.region,
        "AWS_DEFAULT_REGION": resources.region,
        "JOBS_TABLE_NAME": resources.jobs_table,
        "HEALTH_TABLE_NAME": resources.health_table,
        "DEVICES_TABLE_NAME": resources.devices_table,
        "SCRAPE_QUEUE_URL": resources.scrape_queue_url,
        "NOTIFICATION_TOPIC_ARN": resources.notification_topic_arn,
        "APP_BASE_URL": "http://localhost:5173",
        "SHARD_SIZE": "8",
        # Local runs print notifications instead of delivering them.
        "EMAIL_TRANSPORT": "console",
        "PUSH_TRANSPORT": "console",
        "API_WRITE_TOKEN": "local-dev-token",
        "LOG_LEVEL": "INFO",
        "PYTHONUNBUFFERED": "1",
    }


def provision_functions(resources: Resources) -> None:
    lam = client("lambda", resources.endpoint_url, resources.region)
    code = bundle_zip()
    environment = {"Variables": function_environment(resources)}

    for name, handler in FUNCTIONS:
        try:
            created = lam.create_function(
                FunctionName=name,
                Runtime="python3.11",
                Role=resources.lambda_role_arn,
                Handler=handler,
                Code={"ZipFile": code},
                Timeout=WORKER_TIMEOUT,
                MemorySize=512,
                Environment=environment,
                Publish=True,
            )
            resources.functions[name] = str(created["FunctionArn"])
        except lam.exceptions.ResourceConflictException:
            lam.update_function_code(FunctionName=name, ZipFile=code, Publish=True)
            _wait_for_function(lam, name)
            lam.update_function_configuration(
                FunctionName=name,
                Handler=handler,
                Timeout=WORKER_TIMEOUT,
                Environment=environment,
            )
            resources.functions[name] = str(
                lam.get_function(FunctionName=name)["Configuration"]["FunctionArn"]
            )
        _wait_for_function(lam, name)
        print(f"  function: {name} -> {handler}")


def _wait_for_function(lam: Any, name: str, *, timeout: float = 120.0) -> None:
    """Wait until a function is Active; updates are asynchronous in LocalStack."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        config = lam.get_function(FunctionName=name)["Configuration"]
        state = config.get("State", "Active")
        last_update = config.get("LastUpdateStatus", "Successful")
        if state == "Active" and last_update in {"Successful", None}:
            return
        if state == "Failed" or last_update == "Failed":
            raise SystemExit(
                f"{name} failed to become active: {config.get('StateReason')} "
                f"/ {config.get('LastUpdateStatusReason')}"
            )
        time.sleep(1.0)
    raise SystemExit(f"{name} was not Active within {timeout:.0f}s")


def provision_event_sources(resources: Resources) -> None:
    lam = client("lambda", resources.endpoint_url, resources.region)
    sqs = client("sqs", resources.endpoint_url, resources.region)

    def arn_of(queue_url: str) -> str:
        return str(
            sqs.get_queue_attributes(QueueUrl=queue_url, AttributeNames=["QueueArn"])["Attributes"][
                "QueueArn"
            ]
        )

    wiring = (
        ("jobmonitor-worker", resources.scrape_queue_url, 1),
        ("jobmonitor-notifier", resources.notification_queue_url, 5),
    )
    for function_name, queue_url, batch_size in wiring:
        queue_arn = arn_of(queue_url)
        existing = lam.list_event_source_mappings(
            FunctionName=function_name, EventSourceArn=queue_arn
        ).get("EventSourceMappings", [])
        if existing:
            print(f"  event source: {queue_arn.rsplit(':', 1)[-1]} -> {function_name} (exists)")
            continue
        lam.create_event_source_mapping(
            EventSourceArn=queue_arn,
            FunctionName=function_name,
            BatchSize=batch_size,
            Enabled=True,
            # The same partial-batch contract the CDK stack configures.
            FunctionResponseTypes=["ReportBatchItemFailures"],
        )
        print(f"  event source: {queue_arn.rsplit(':', 1)[-1]} -> {function_name}")


def provision_api(resources: Resources) -> None:
    """A REST API with a Lambda proxy integration in front of the API Lambda.

    Deliberately API Gateway **v1** (REST), not v2 (HTTP API): v2 is a LocalStack
    Pro feature, while v1 is in the community edition. The deployed stack uses an
    HTTP API because it is cheaper and simpler, so this is one of the documented
    local/real differences - but both invoke the same handler with the same proxy
    event shape, and `to_request` reads v1 and v2 events alike. Recorded in
    ARCHITECTURE.md -> "Local emulation gaps".
    """
    try:
        apigw = client("apigateway", resources.endpoint_url, resources.region)
        existing = [
            api for api in apigw.get_rest_apis().get("items", []) if api.get("name") == API_NAME
        ]
        api_id = (
            str(existing[0]["id"]) if existing else str(apigw.create_rest_api(name=API_NAME)["id"])
        )

        resource_ids = {
            item.get("path"): item["id"] for item in apigw.get_resources(restApiId=api_id)["items"]
        }
        root = resource_ids["/"]
        proxy = (
            resource_ids.get("/{proxy+}")
            or apigw.create_resource(restApiId=api_id, parentId=root, pathPart="{proxy+}")["id"]
        )

        function_arn = resources.functions["jobmonitor-api"]
        uri = (
            f"arn:aws:apigateway:{resources.region}:lambda:path/2015-03-31"
            f"/functions/{function_arn}/invocations"
        )
        # ANY on both the root and the greedy proxy, so "/" and "/jobs/x" route.
        for resource_id in (root, proxy):
            with contextlib.suppress(Exception):
                apigw.put_method(
                    restApiId=api_id,
                    resourceId=resource_id,
                    httpMethod="ANY",
                    authorizationType="NONE",
                )
            apigw.put_integration(
                restApiId=api_id,
                resourceId=resource_id,
                httpMethod="ANY",
                type="AWS_PROXY",
                integrationHttpMethod="POST",
                uri=uri,
            )
        apigw.create_deployment(restApiId=api_id, stageName=API_STAGE)

        resources.api_id = api_id
        host = resources.endpoint_url.split("://", 1)[-1]
        resources.api_url = f"http://{host}/restapis/{api_id}/{API_STAGE}/_user_request_"
        print(f"  rest api: {api_id} -> {resources.api_url}")
    except Exception as exc:
        # The Level-7 test also invokes the API Lambda directly with a real proxy
        # event, so the feed path is proven even when the gateway is unavailable.
        print(f"  rest api: SKIPPED ({type(exc).__name__}: {exc})")
        resources.api_id = ""
        resources.api_url = ""


def provision(endpoint_url: str, region: str = "us-east-1") -> Resources:
    resources = Resources(endpoint_url=endpoint_url, region=region)
    print(f"provisioning the architecture in {endpoint_url}")
    provision_tables(resources)
    provision_queues(resources)
    provision_topic(resources)
    provision_role(resources)
    provision_functions(resources)
    provision_event_sources(resources)
    provision_api(resources)
    path = resources.save()
    print(f"\nresource ids -> {path.relative_to(REPO_ROOT)}")
    return resources


# --------------------------------------------------------------------- teardown


def teardown(endpoint_url: str, region: str = "us-east-1") -> None:
    import boto3
    from botocore.exceptions import ClientError

    print(f"tearing down the architecture in {endpoint_url}")
    lam = client("lambda", endpoint_url, region)
    for name, _handler in FUNCTIONS:
        try:
            for mapping in lam.list_event_source_mappings(FunctionName=name).get(
                "EventSourceMappings", []
            ):
                lam.delete_event_source_mapping(UUID=mapping["UUID"])
            lam.delete_function(FunctionName=name)
        except ClientError:
            pass

    sqs = client("sqs", endpoint_url, region)
    for queue in (SCRAPE_QUEUE, SCRAPE_DLQ, NOTIFICATION_QUEUE, NOTIFICATION_DLQ):
        with contextlib.suppress(ClientError):
            sqs.delete_queue(QueueUrl=sqs.get_queue_url(QueueName=queue)["QueueUrl"])

    sns = client("sns", endpoint_url, region)
    try:
        for topic in sns.list_topics().get("Topics", []):
            if topic["TopicArn"].endswith(NOTIFICATION_TOPIC):
                sns.delete_topic(TopicArn=topic["TopicArn"])
    except ClientError:
        pass

    dynamodb = boto3.resource(
        "dynamodb",
        region_name=region,
        endpoint_url=endpoint_url,
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    delete_tables(dynamodb, Resources(endpoint_url=endpoint_url).to_settings())

    if RESOURCES_PATH.exists():
        RESOURCES_PATH.unlink()
    print("done")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:4566")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--teardown", action="store_true")
    parser.add_argument("--print", dest="show", action="store_true")
    args = parser.parse_args(argv)

    if args.show:
        print(json.dumps(asdict(Resources.load()), indent=2))
        return 0
    if args.teardown:
        teardown(args.url, args.region)
        return 0

    resources = provision(args.url, args.region)
    print(json.dumps({"functions": sorted(resources.functions), "api_id": resources.api_id}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
