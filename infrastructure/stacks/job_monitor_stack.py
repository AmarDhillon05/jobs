"""The deployable stack (PRD §21).

    EventBridge rule (rate: 10 min)
            |
            v
    Coordinator Lambda  -- shards companies.json
            |
            v
      SQS scrape queue  --after 3 attempts-->  scrape DLQ
         /    |    \\
     Worker Lambdas (concurrent, one shard each)
            |
            +--> DynamoDB jobs + scraper-health tables
            |
            v
      SNS notification topic
            |
            v
    SQS notification queue --after 3 attempts--> notification DLQ
            |
            v
      Notifier Lambda ---> SES email + SNS/Web Push

    API Gateway HTTP API --> API Lambda --> DynamoDB --> the PWA

Notes on the choices this file encodes:

* **Table schemas are imported, not retyped.** ``jobmonitor.storage.tables``
  declares them, and ``infrastructure/tests`` asserts the synthesized template
  matches - so an index renamed in the query code cannot silently disagree with
  the deployed index.
* **The Lambda asset is just ``src/`` + ``companies.json``.** No dependencies to
  install means synth needs neither Docker nor network.
* **Report-batch-item-failures is enabled** on both event sources. Without it a
  single poison message replays its whole batch; with it only the failing message
  retries and then reaches the DLQ.
* **IAM is granted per function, not shared.** The coordinator can enqueue and
  nothing else; the API can read jobs and cannot write them.
* **EventBridge *rule*, not EventBridge Scheduler.** Identical function for a
  fixed-rate trigger, materially better local emulation, and one less service to
  reason about. Recorded in ARCHITECTURE.md.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    Tags,
)
from aws_cdk import aws_apigatewayv2 as apigw
from aws_cdk import aws_apigatewayv2_integrations as apigw_integrations
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as events_targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_lambda_event_sources as event_sources
from aws_cdk import aws_logs as logs
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as sns_subscriptions
from aws_cdk import aws_sqs as sqs
from constructs import Construct

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from build_lambda_bundle import build as build_lambda_bundle  # noqa: E402
from jobmonitor.storage.dynamo import (  # noqa: E402
    COMPANY_INDEX,
    PENDING_INDEX,
    RECENT_INDEX,
)
from jobmonitor.storage.tables import (  # noqa: E402
    HEALTH_TTL_ATTRIBUTE,
    PENDING_KEY,
)

RUNTIME = lambda_.Runtime.PYTHON_3_12
#: Long enough for a shard of 8 companies with retries; far short of the 15 min max.
WORKER_TIMEOUT = Duration.minutes(5)
POLL_INTERVAL = Duration.minutes(10)
#: The email digest: every hour at minute 2 (off the top of the hour, where
#: scheduled invocations bunch up). The handler floors to the hour, so each run
#: covers exactly the previous clock hour.
DIGEST_SCHEDULE = events.Schedule.cron(minute="2", hour="*")
DIGEST_WINDOW_MINUTES = 60
#: Attempts before a message is dead-lettered.
MAX_RECEIVE_COUNT = 3
LOG_RETENTION = logs.RetentionDays.TWO_WEEKS


class JobMonitorStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        shard_size: int = 8,
        notify_threshold: int = 55,
        keep_threshold: int = 35,
        email_from: str = "alerts@example.com",
        email_to: str = "you@example.com",
        app_base_url: str = "https://app.example.com",
        ntfy_topic: str | None = None,
        ntfy_server: str = "https://ntfy.sh",
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        Tags.of(self).add("project", "internship-job-monitor")

        code = self._lambda_code()

        # ------------------------------------------------------------- storage
        self.jobs_table = self._jobs_table()
        self.health_table = self._health_table()
        self.devices_table = self._devices_table()

        # -------------------------------------------------------------- queues
        self.scrape_dlq = sqs.Queue(
            self,
            "ScrapeDlq",
            retention_period=Duration.days(14),
            enforce_ssl=True,
        )
        self.scrape_queue = sqs.Queue(
            self,
            "ScrapeQueue",
            # Must exceed the worker timeout, or SQS re-delivers a message that
            # is still being processed and the same shard runs twice.
            visibility_timeout=Duration.seconds(WORKER_TIMEOUT.to_seconds() * 2),
            retention_period=Duration.days(1),
            enforce_ssl=True,
            dead_letter_queue=sqs.DeadLetterQueue(
                max_receive_count=MAX_RECEIVE_COUNT, queue=self.scrape_dlq
            ),
        )

        self.notification_topic = sns.Topic(
            self, "NotificationTopic", display_name="New internships"
        )
        self.notification_dlq = sqs.Queue(
            self, "NotificationDlq", retention_period=Duration.days(14), enforce_ssl=True
        )
        self.notification_queue = sqs.Queue(
            self,
            "NotificationQueue",
            visibility_timeout=Duration.minutes(2),
            retention_period=Duration.days(1),
            enforce_ssl=True,
            dead_letter_queue=sqs.DeadLetterQueue(
                max_receive_count=MAX_RECEIVE_COUNT, queue=self.notification_dlq
            ),
        )
        # A topic in front of the queue keeps the fan-out open: a second consumer
        # (a log sink, a webhook) is a subscription, not a code change.
        self.notification_topic.add_subscription(
            sns_subscriptions.SqsSubscription(self.notification_queue, raw_message_delivery=False)
        )

        # ----------------------------------------------------------- functions
        common_env = {
            "JOBS_TABLE_NAME": self.jobs_table.table_name,
            "HEALTH_TABLE_NAME": self.health_table.table_name,
            "DEVICES_TABLE_NAME": self.devices_table.table_name,
            "APP_BASE_URL": app_base_url,
            "NOTIFY_RELEVANCE_THRESHOLD": str(notify_threshold),
            "KEEP_RELEVANCE_THRESHOLD": str(keep_threshold),
            "SHARD_SIZE": str(shard_size),
            "POLL_INTERVAL_MINUTES": str(POLL_INTERVAL.to_minutes()),
            # Python buffers stdout by default, which loses log lines on timeout.
            "PYTHONUNBUFFERED": "1",
        }

        self.coordinator = self._function(
            "Coordinator",
            code,
            "jobmonitor.orchestration.handlers.coordinator_handler",
            timeout=Duration.minutes(1),
            memory=256,
            environment={**common_env, "SCRAPE_QUEUE_URL": self.scrape_queue.queue_url},
        )
        self.scrape_queue.grant_send_messages(self.coordinator)

        self.worker = self._function(
            "Worker",
            code,
            "jobmonitor.orchestration.handlers.worker_handler",
            timeout=WORKER_TIMEOUT,
            memory=512,
            environment={
                **common_env,
                "SCRAPE_QUEUE_URL": self.scrape_queue.queue_url,
                "NOTIFICATION_TOPIC_ARN": self.notification_topic.topic_arn,
                "HTTP_MAX_ATTEMPTS": "3",
                "HTTP_TIMEOUT_SECONDS": "15",
            },
            # Bounded so 19 shards cannot become 19 simultaneous bursts against
            # the same ATS providers, and so one runaway poll cannot exhaust the
            # account's concurrency.
            reserved_concurrency=10,
        )
        self.jobs_table.grant_read_write_data(self.worker)
        self.health_table.grant_read_write_data(self.worker)
        self.notification_topic.grant_publish(self.worker)
        self.worker.add_event_source(
            event_sources.SqsEventSource(
                self.scrape_queue,
                batch_size=1,  # one shard per invocation: clean retry semantics
                report_batch_item_failures=True,
            )
        )

        email_env = {
            "EMAIL_TRANSPORT": "ses",
            "EMAIL_FROM": email_from,
            "EMAIL_TO": email_to,
            # Instant alerts go to push; email is the hourly digest below.
            "EMAIL_MODE": "digest",
            "EMAIL_DIGEST_MINUTES": str(DIGEST_WINDOW_MINUTES),
        }
        # ntfy when a topic is given (the recommended personal setup), otherwise
        # SNS, which needs nothing outside AWS.
        push_env = (
            {"PUSH_TRANSPORT": "ntfy", "NTFY_TOPIC": ntfy_topic, "NTFY_SERVER": ntfy_server}
            if ntfy_topic
            else {"PUSH_TRANSPORT": "sns"}
        )
        self.notifier = self._function(
            "Notifier",
            code,
            "jobmonitor.orchestration.handlers.notifier_handler",
            timeout=Duration.minutes(1),
            memory=256,
            environment={
                **common_env,
                **email_env,
                **push_env,
                "NOTIFICATION_TOPIC_ARN": self.notification_topic.topic_arn,
            },
        )
        self.jobs_table.grant_read_write_data(self.notifier)
        self.devices_table.grant_read_data(self.notifier)
        self.notification_topic.grant_publish(self.notifier)
        ses_send = iam.PolicyStatement(
            actions=["ses:SendEmail", "ses:SendRawEmail"],
            # Narrowed to the one verified identity we send as.
            resources=[
                f"arn:{self.partition}:ses:{self.region}:{self.account}:identity/{email_from}"
            ],
        )
        self.notifier.add_to_role_policy(ses_send)
        self.notifier.add_event_source(
            event_sources.SqsEventSource(
                self.notification_queue, batch_size=5, report_batch_item_failures=True
            )
        )

        self.api_function = self._function(
            "Api",
            code,
            "jobmonitor.api.lambda_handler.handler",
            timeout=Duration.seconds(15),
            memory=256,
            environment=common_env,
        )
        # Read-only on jobs: the API must not be able to mutate discovered jobs.
        self.jobs_table.grant_read_data(self.api_function)
        self.health_table.grant_read_data(self.api_function)
        self.devices_table.grant_read_write_data(self.api_function)
        for index in (RECENT_INDEX, COMPANY_INDEX, PENDING_INDEX):
            self.api_function.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["dynamodb:Query"],
                    resources=[f"{self.jobs_table.table_arn}/index/{index}"],
                )
            )

        self.digest = self._function(
            "Digest",
            code,
            "jobmonitor.orchestration.handlers.digest_handler",
            timeout=Duration.minutes(1),
            memory=256,
            environment={**common_env, **email_env},
        )
        # Reads the recent-jobs index and sends one email: nothing else.
        self.jobs_table.grant_read_data(self.digest)
        self.digest.add_to_role_policy(
            iam.PolicyStatement(
                actions=["dynamodb:Query"],
                resources=[f"{self.jobs_table.table_arn}/index/{RECENT_INDEX}"],
            )
        )
        self.digest.add_to_role_policy(ses_send)

        # -------------------------------------------------------------- trigger
        self.schedule = events.Rule(
            self,
            "PollSchedule",
            description="Poll every company for newly posted internships",
            schedule=events.Schedule.rate(POLL_INTERVAL),
            targets=[
                events_targets.LambdaFunction(
                    self.coordinator,
                    event=events.RuleTargetInput.from_object(
                        {"detail": {"poll_id": "scheduled", "dry_run": False}}
                    ),
                    retry_attempts=2,
                )
            ],
        )

        self.digest_schedule = events.Rule(
            self,
            "DigestSchedule",
            description="Email everything first seen in the previous hour (nothing if empty)",
            schedule=DIGEST_SCHEDULE,
            # No input override: the handler reads the event's own `time`, so a
            # retried invocation covers the same hour as the attempt it retries.
            targets=[events_targets.LambdaFunction(self.digest, retry_attempts=2)],
        )

        # ------------------------------------------------------------------ api
        self.http_api = apigw.HttpApi(
            self,
            "JobsApi",
            description="Read API for the internship monitor client",
            cors_preflight=apigw.CorsPreflightOptions(
                allow_origins=["*"],
                allow_methods=[
                    apigw.CorsHttpMethod.GET,
                    apigw.CorsHttpMethod.POST,
                    apigw.CorsHttpMethod.DELETE,
                    apigw.CorsHttpMethod.OPTIONS,
                ],
                allow_headers=["Content-Type", "X-Api-Token"],
            ),
        )
        self.http_api.add_routes(
            path="/{proxy+}",
            methods=[
                apigw.HttpMethod.GET,
                apigw.HttpMethod.POST,
                apigw.HttpMethod.DELETE,
                apigw.HttpMethod.OPTIONS,
            ],
            integration=apigw_integrations.HttpLambdaIntegration(
                "ApiIntegration", handler=self.api_function
            ),
        )

        self._alarms()
        self._outputs()

    # ------------------------------------------------------------------ helpers
    def _lambda_code(self) -> lambda_.Code:
        """The deployment asset: ``src/jobmonitor`` + ``companies.json``.

        Built here at synth time so the asset can never be stale relative to the
        code being synthesized.
        """
        bundle = build_lambda_bundle()
        return lambda_.Code.from_asset(str(bundle))

    def _function(
        self,
        name: str,
        code: lambda_.Code,
        handler: str,
        *,
        timeout: Duration,
        memory: int,
        environment: dict[str, str],
        reserved_concurrency: int | None = None,
    ) -> lambda_.Function:
        kwargs: dict[str, Any] = {
            "runtime": RUNTIME,
            "code": code,
            "handler": handler,
            "timeout": timeout,
            "memory_size": memory,
            "environment": environment,
            # An explicit log group rather than the deprecated `log_retention`
            # prop, which provisions a custom resource to do the same thing.
            "log_group": logs.LogGroup(
                self,
                f"{name}LogGroup",
                retention=LOG_RETENTION,
                removal_policy=RemovalPolicy.DESTROY,
            ),
            # ARM is ~20% cheaper per GB-second at identical performance here.
            "architecture": lambda_.Architecture.ARM_64,
            "description": f"{name} for the internship job monitor",
        }
        if reserved_concurrency is not None:
            kwargs["reserved_concurrent_executions"] = reserved_concurrency
        return lambda_.Function(self, f"{name}Function", **kwargs)

    def _jobs_table(self) -> dynamodb.Table:
        table = dynamodb.Table(
            self,
            "JobsTable",
            partition_key=dynamodb.Attribute(name="job_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            # The jobs table is the system's memory: destroying it would make
            # every known job look new and re-notify the lot.
            removal_policy=RemovalPolicy.RETAIN,
        )
        table.add_global_secondary_index(
            index_name=RECENT_INDEX,
            partition_key=dynamodb.Attribute(name="feed", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="first_seen", type=dynamodb.AttributeType.STRING),
        )
        table.add_global_secondary_index(
            index_name=COMPANY_INDEX,
            partition_key=dynamodb.Attribute(name="company", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="first_seen", type=dynamodb.AttributeType.STRING),
        )
        table.add_global_secondary_index(
            # Sparse: the key attribute exists only while an alert is outstanding.
            index_name=PENDING_INDEX,
            partition_key=dynamodb.Attribute(name=PENDING_KEY, type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="first_seen", type=dynamodb.AttributeType.STRING),
        )
        return table

    def _health_table(self) -> dynamodb.Table:
        return dynamodb.Table(
            self,
            "HealthTable",
            partition_key=dynamodb.Attribute(
                name="scraper_key", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            # Diagnostics, not records: they expire and the table is disposable.
            time_to_live_attribute=HEALTH_TTL_ATTRIBUTE,
            removal_policy=RemovalPolicy.DESTROY,
        )

    def _devices_table(self) -> dynamodb.Table:
        return dynamodb.Table(
            self,
            "DevicesTable",
            partition_key=dynamodb.Attribute(name="device_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )

    def _alarms(self) -> None:
        """Alarm on the things that mean "alerts are silently not arriving"."""
        cloudwatch.Alarm(
            self,
            "ScrapeDlqNotEmpty",
            alarm_description="A scrape shard failed permanently and was dead-lettered",
            metric=self.scrape_dlq.metric_approximate_number_of_messages_visible(
                period=Duration.minutes(5)
            ),
            threshold=1,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        cloudwatch.Alarm(
            self,
            "NotificationDlqNotEmpty",
            alarm_description="A notification failed permanently - the user was not alerted",
            metric=self.notification_dlq.metric_approximate_number_of_messages_visible(
                period=Duration.minutes(5)
            ),
            threshold=1,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        cloudwatch.Alarm(
            self,
            "CoordinatorFailing",
            alarm_description="The coordinator is erroring: no polling is happening at all",
            metric=self.coordinator.metric_errors(period=Duration.minutes(30)),
            threshold=2,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        cloudwatch.Alarm(
            self,
            "WorkersFailing",
            alarm_description="Workers are erroring frequently",
            metric=self.worker.metric_errors(period=Duration.hours(1)),
            threshold=20,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        cloudwatch.Alarm(
            self,
            "DigestFailing",
            alarm_description="The hourly email digest is failing: summaries are not arriving",
            metric=self.digest.metric_errors(period=Duration.hours(3)),
            threshold=2,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )

    def _outputs(self) -> None:
        CfnOutput(self, "JobsTableName", value=self.jobs_table.table_name)
        CfnOutput(self, "HealthTableName", value=self.health_table.table_name)
        CfnOutput(self, "DevicesTableName", value=self.devices_table.table_name)
        CfnOutput(self, "ScrapeQueueUrl", value=self.scrape_queue.queue_url)
        CfnOutput(self, "ScrapeDlqUrl", value=self.scrape_dlq.queue_url)
        CfnOutput(self, "NotificationTopicArn", value=self.notification_topic.topic_arn)
        CfnOutput(self, "NotificationQueueUrl", value=self.notification_queue.queue_url)
        CfnOutput(self, "NotificationDlqUrl", value=self.notification_dlq.queue_url)
        CfnOutput(
            self,
            "ApiUrl",
            value=self.http_api.api_endpoint,
            description="Set this as VITE_API_BASE_URL when building the client",
        )


__all__ = ["JobMonitorStack"]
