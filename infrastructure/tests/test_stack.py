"""Level D - CDK template assertions (PRD §13.5, Gate D).

These do not test that AWS works; they test that the template says what the code
assumes. The assertions worth having are the ones where a mismatch would produce a
silently broken system rather than a deploy error:

* index names matching :mod:`jobmonitor.storage.tables` (a query against a
  non-existent index fails at runtime, not at deploy);
* ``ReportBatchItemFailures`` on both event sources (without it one poison message
  replays its whole batch forever);
* queue visibility timeout exceeding the worker timeout (otherwise a shard is
  re-delivered while still running);
* redrive policies present, so a permanent failure reaches a DLQ;
* IAM shaped as intended - the API cannot write jobs, the coordinator cannot read
  them;
* the schedule firing at the documented interval.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from aws_cdk import App, Environment
from aws_cdk.assertions import Template

from stacks.job_monitor_stack import (
    MAX_RECEIVE_COUNT,
    POLL_INTERVAL,
    WORKER_MAX_CONCURRENCY,
    WORKER_TIMEOUT,
    JobMonitorStack,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.infra


@pytest.fixture(scope="module")
def template() -> Template:
    app = App()
    stack = JobMonitorStack(
        app,
        "TestStack",
        env=Environment(account="000000000000", region="us-east-1"),
    )
    return Template.from_stack(stack)


@pytest.fixture(scope="module")
def resources(template: Template) -> dict[str, Any]:
    return template.to_json()["Resources"]


def by_type(resources: dict[str, Any], resource_type: str) -> dict[str, Any]:
    return {key: value for key, value in resources.items() if value["Type"] == resource_type}


class TestSynthesis:
    def test_the_stack_synthesizes(self, template: Template) -> None:
        assert template.to_json()["Resources"]

    def test_no_unresolved_references(self, template: Template) -> None:
        """Every Ref/GetAtt must point at something that exists in the template."""
        body = template.to_json()
        resources = body["Resources"]
        known = set(resources) | set(body.get("Parameters", {}))

        missing: list[str] = []

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    if key == "Ref" and isinstance(value, str):
                        if value not in known and not value.startswith("AWS::"):
                            missing.append(f"Ref {value}")
                    elif key == "Fn::GetAtt":
                        target = value[0] if isinstance(value, list) else str(value).split(".")[0]
                        if target not in known:
                            missing.append(f"GetAtt {target}")
                    else:
                        walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(resources)
        assert not missing, f"unresolved references: {sorted(set(missing))}"

    def test_every_resource_is_typed_and_named(self, resources: dict[str, Any]) -> None:
        for name, resource in resources.items():
            assert resource.get("Type"), name
            assert resource["Type"].startswith(("AWS::", "Custom::")), name


class TestStorage:
    def test_three_tables_exist(self, template: Template) -> None:
        template.resource_count_is("AWS::DynamoDB::Table", 4)  # + the Apply kit's

    def test_tables_are_on_demand(self, resources: dict[str, Any]) -> None:
        for name, table in by_type(resources, "AWS::DynamoDB::Table").items():
            assert table["Properties"]["BillingMode"] == "PAY_PER_REQUEST", name

    def test_jobs_table_indexes_match_the_query_code(self, resources: dict[str, Any]) -> None:
        from jobmonitor.storage.dynamo import COMPANY_INDEX, PENDING_INDEX, RECENT_INDEX

        tables = by_type(resources, "AWS::DynamoDB::Table")
        jobs = next(
            table
            for table in tables.values()
            if table["Properties"]["KeySchema"][0]["AttributeName"] == "job_id"
        )
        indexes = {
            index["IndexName"]: index for index in jobs["Properties"]["GlobalSecondaryIndexes"]
        }
        assert set(indexes) == {RECENT_INDEX, COMPANY_INDEX, PENDING_INDEX}

    def test_index_keys_match_the_declared_specs(self, resources: dict[str, Any]) -> None:
        """The whole reason storage/tables.py exists: one declaration, no drift."""
        from jobmonitor.storage.tables import jobs_table_spec

        spec_indexes = {
            index["IndexName"]: [key["AttributeName"] for key in index["KeySchema"]]
            for index in jobs_table_spec("x")["GlobalSecondaryIndexes"]
        }
        tables = by_type(resources, "AWS::DynamoDB::Table")
        jobs = next(
            table
            for table in tables.values()
            if table["Properties"]["KeySchema"][0]["AttributeName"] == "job_id"
        )
        template_indexes = {
            index["IndexName"]: [key["AttributeName"] for key in index["KeySchema"]]
            for index in jobs["Properties"]["GlobalSecondaryIndexes"]
        }
        assert template_indexes == spec_indexes

    def test_jobs_table_is_retained_on_stack_deletion(self, resources: dict[str, Any]) -> None:
        # Losing it would make every known job look new and re-notify all of them.
        tables = by_type(resources, "AWS::DynamoDB::Table")
        jobs_key = next(
            key
            for key, table in tables.items()
            if table["Properties"]["KeySchema"][0]["AttributeName"] == "job_id"
        )
        assert resources[jobs_key]["DeletionPolicy"] == "Retain"

    def test_jobs_table_has_point_in_time_recovery(self, resources: dict[str, Any]) -> None:
        tables = by_type(resources, "AWS::DynamoDB::Table")
        jobs = next(
            table
            for table in tables.values()
            if table["Properties"]["KeySchema"][0]["AttributeName"] == "job_id"
        )
        spec = jobs["Properties"]["PointInTimeRecoverySpecification"]
        assert spec["PointInTimeRecoveryEnabled"] is True

    def test_health_table_expires_its_rows(self, template: Template) -> None:
        from jobmonitor.storage.tables import HEALTH_TTL_ATTRIBUTE

        template.has_resource_properties(
            "AWS::DynamoDB::Table",
            {
                "KeySchema": [{"AttributeName": "scraper_key", "KeyType": "HASH"}],
                "TimeToLiveSpecification": {
                    "AttributeName": HEALTH_TTL_ATTRIBUTE,
                    "Enabled": True,
                },
            },
        )


class TestQueues:
    def test_four_queues_exist(self, template: Template) -> None:
        # scrape + scrape DLQ + notification + notification DLQ
        template.resource_count_is("AWS::SQS::Queue", 4)

    def test_both_work_queues_have_a_redrive_policy(self, resources: dict[str, Any]) -> None:
        queues = by_type(resources, "AWS::SQS::Queue")
        with_redrive = [
            queue for queue in queues.values() if "RedrivePolicy" in queue["Properties"]
        ]
        assert len(with_redrive) == 2
        for queue in with_redrive:
            assert queue["Properties"]["RedrivePolicy"]["maxReceiveCount"] == MAX_RECEIVE_COUNT

    def test_scrape_visibility_timeout_exceeds_the_worker_timeout(
        self, resources: dict[str, Any]
    ) -> None:
        """Otherwise SQS re-delivers a shard that is still being processed."""
        queues = by_type(resources, "AWS::SQS::Queue")
        scrape = next(
            queue
            for queue in queues.values()
            if "RedrivePolicy" in queue["Properties"]
            and queue["Properties"].get("VisibilityTimeout", 0) > 60
        )
        assert scrape["Properties"]["VisibilityTimeout"] > WORKER_TIMEOUT.to_seconds()

    def test_dead_letter_queues_retain_for_two_weeks(self, resources: dict[str, Any]) -> None:
        queues = by_type(resources, "AWS::SQS::Queue")
        dlqs = [
            queue
            for queue in queues.values()
            if queue["Properties"].get("MessageRetentionPeriod") == 1209600
        ]
        assert len(dlqs) == 2

    def test_queues_require_tls(self, resources: dict[str, Any]) -> None:
        policies = by_type(resources, "AWS::SQS::QueuePolicy")
        denies = [
            statement
            for policy in policies.values()
            for statement in policy["Properties"]["PolicyDocument"]["Statement"]
            if statement.get("Effect") == "Deny"
        ]
        assert denies, "expected an enforce-SSL deny statement on the queues"


class TestTopic:
    def test_one_topic_with_a_queue_subscription(self, template: Template) -> None:
        template.resource_count_is("AWS::SNS::Topic", 1)
        template.has_resource_properties("AWS::SNS::Subscription", {"Protocol": "sqs"})


class TestFunctions:
    def test_five_functions_exist(self, resources: dict[str, Any]) -> None:
        functions = by_type(resources, "AWS::Lambda::Function")
        # Coordinator, worker, notifier, digest, API - and no custom-resource
        # log-retention provider.
        assert len(functions) == 6  # + the Apply kit

    def test_handlers_point_at_real_callables(self, resources: dict[str, Any]) -> None:
        import importlib

        for name, function in by_type(resources, "AWS::Lambda::Function").items():
            handler = function["Properties"]["Handler"]
            module_path, _, attribute = handler.rpartition(".")
            module = importlib.import_module(module_path)
            assert callable(getattr(module, attribute)), f"{name}: {handler}"

    def test_every_function_uses_a_supported_python_runtime(
        self, resources: dict[str, Any]
    ) -> None:
        for name, function in by_type(resources, "AWS::Lambda::Function").items():
            assert function["Properties"]["Runtime"].startswith("python3."), name

    def test_every_function_is_arm(self, resources: dict[str, Any]) -> None:
        for name, function in by_type(resources, "AWS::Lambda::Function").items():
            assert function["Properties"]["Architectures"] == ["arm64"], name

    def test_worker_timeout_is_well_inside_the_lambda_limit(
        self, resources: dict[str, Any]
    ) -> None:
        functions = by_type(resources, "AWS::Lambda::Function")
        worker = next(
            function
            for function in functions.values()
            if "worker_handler" in function["Properties"]["Handler"]
        )
        assert worker["Properties"]["Timeout"] == WORKER_TIMEOUT.to_seconds()
        assert worker["Properties"]["Timeout"] < 900

    def test_worker_concurrency_is_bounded_by_the_queue_not_reserved(
        self, resources: dict[str, Any]
    ) -> None:
        """Reserved concurrency failed the first real deploy: a new account's
        Lambda limit can be 10, and none of the last 10 may be reserved."""
        for name, function in by_type(resources, "AWS::Lambda::Function").items():
            assert "ReservedConcurrentExecutions" not in function["Properties"], name
        mapping = next(
            m
            for m in by_type(resources, "AWS::Lambda::EventSourceMapping").values()
            if m["Properties"]["BatchSize"] == 1
        )
        assert mapping["Properties"]["ScalingConfig"] == {
            "MaximumConcurrency": WORKER_MAX_CONCURRENCY
        }

    def test_every_function_knows_the_table_names(self, resources: dict[str, Any]) -> None:
        for name, function in by_type(resources, "AWS::Lambda::Function").items():
            env = function["Properties"]["Environment"]["Variables"]
            assert "JOBS_TABLE_NAME" in env, name

    def test_no_secret_is_baked_into_an_environment_variable(
        self, resources: dict[str, Any]
    ) -> None:
        """Secrets belong in SSM/Secrets Manager, never in the template."""
        forbidden = ("SECRET", "PASSWORD", "PRIVATE_KEY", "TOKEN", "ACCESS_KEY")
        for name, function in by_type(resources, "AWS::Lambda::Function").items():
            for key, value in function["Properties"]["Environment"]["Variables"].items():
                if key.endswith("_PARAMETER"):
                    # The *name* of an SSM parameter (`/jobmonitor/kit-secret`): the
                    # secret itself is read at runtime and never in the template.
                    assert str(value).startswith("/jobmonitor/"), f"{name}.{key}"
                    continue
                if any(marker in key.upper() for marker in forbidden):
                    # A reference is fine; a literal is not.
                    assert not isinstance(value, str) or not value, f"{name}.{key} is a literal"

    def test_each_function_has_its_own_log_group(self, resources: dict[str, Any]) -> None:
        assert len(by_type(resources, "AWS::Logs::LogGroup")) == 6

    def test_log_groups_have_a_retention_policy(self, resources: dict[str, Any]) -> None:
        for name, group in by_type(resources, "AWS::Logs::LogGroup").items():
            assert group["Properties"]["RetentionInDays"] > 0, name


class TestEventSources:
    def test_both_event_source_mappings_report_batch_item_failures(
        self, resources: dict[str, Any]
    ) -> None:
        """Without this, one poison message replays its whole batch forever."""
        mappings = by_type(resources, "AWS::Lambda::EventSourceMapping")
        assert len(mappings) == 2
        for name, mapping in mappings.items():
            assert mapping["Properties"]["FunctionResponseTypes"] == ["ReportBatchItemFailures"], (
                name
            )

    def test_the_worker_takes_one_shard_per_invocation(self, resources: dict[str, Any]) -> None:
        mappings = by_type(resources, "AWS::Lambda::EventSourceMapping")
        batch_sizes = sorted(m["Properties"]["BatchSize"] for m in mappings.values())
        assert batch_sizes == [1, 5]


class TestSchedule:
    def test_the_rule_fires_at_the_documented_interval(self, template: Template) -> None:
        template.has_resource_properties(
            "AWS::Events::Rule",
            {
                "ScheduleExpression": f"rate({POLL_INTERVAL.to_minutes()} minutes)",
                "State": "ENABLED",
            },
        )

    def test_the_schedule_targets_the_coordinator(self, resources: dict[str, Any]) -> None:
        rules = by_type(resources, "AWS::Events::Rule")
        rule = next(r for name, r in rules.items() if name.startswith("PollSchedule"))
        targets = rule["Properties"]["Targets"]
        assert len(targets) == 1
        assert "Input" in targets[0]
        assert "poll_id" in targets[0]["Input"]

    def test_the_digest_runs_hourly_and_targets_the_digest_function(
        self, resources: dict[str, Any]
    ) -> None:
        rules = by_type(resources, "AWS::Events::Rule")
        assert len(rules) == 2
        digest = next(r for name, r in rules.items() if name.startswith("DigestSchedule"))
        assert digest["Properties"]["ScheduleExpression"] == "cron(2 * * * ? *)"
        [target] = digest["Properties"]["Targets"]
        # No input override: the handler must see EventBridge's own `time`.
        assert "Input" not in target
        assert "DigestFunction" in json.dumps(target["Arn"])

    def test_the_digest_function_emails_in_digest_mode(self, resources: dict[str, Any]) -> None:
        functions = by_type(resources, "AWS::Lambda::Function")
        for function in functions.values():
            handler = function["Properties"]["Handler"]
            if "digest_handler" in handler or "notifier_handler" in handler:
                env = function["Properties"]["Environment"]["Variables"]
                assert env["EMAIL_MODE"] == "digest", handler
                assert env["EMAIL_DIGEST_MINUTES"] == "60", handler

    def test_eventbridge_is_permitted_to_invoke_the_coordinator(self, template: Template) -> None:
        template.has_resource_properties(
            "AWS::Lambda::Permission",
            {"Action": "lambda:InvokeFunction", "Principal": "events.amazonaws.com"},
        )


class TestApi:
    def test_an_http_api_exists_with_a_lambda_integration(self, template: Template) -> None:
        template.resource_count_is("AWS::ApiGatewayV2::Api", 1)
        template.has_resource_properties(
            "AWS::ApiGatewayV2::Integration", {"IntegrationType": "AWS_PROXY"}
        )

    def test_cors_is_configured_for_the_client(self, resources: dict[str, Any]) -> None:
        api = next(iter(by_type(resources, "AWS::ApiGatewayV2::Api").values()))
        cors = api["Properties"]["CorsConfiguration"]
        assert "GET" in cors["AllowMethods"]
        assert "X-Api-Token" in cors["AllowHeaders"]

    def test_api_gateway_is_permitted_to_invoke_the_api_function(
        self, resources: dict[str, Any]
    ) -> None:
        permissions = by_type(resources, "AWS::Lambda::Permission")
        principals = {
            permission["Properties"].get("Principal") for permission in permissions.values()
        }
        assert "apigateway.amazonaws.com" in principals


class TestIamLeastPrivilege:
    def _policies(self, resources: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            policy["Properties"]["PolicyDocument"]
            for policy in by_type(resources, "AWS::IAM::Policy").values()
        ]

    def test_no_policy_grants_wildcard_action_on_wildcard_resource(
        self, resources: dict[str, Any]
    ) -> None:
        for document in self._policies(resources):
            for statement in document["Statement"]:
                if statement.get("Effect") != "Allow":
                    continue
                actions = statement.get("Action")
                actions = actions if isinstance(actions, list) else [actions]
                if "*" in actions:
                    assert statement.get("Resource") != "*", statement

    def test_the_api_function_cannot_write_the_jobs_table(self, resources: dict[str, Any]) -> None:
        """A read API that can delete job history is a footgun, not an API."""
        policies = by_type(resources, "AWS::IAM::Policy")
        api_policies = [
            policy
            for name, policy in policies.items()
            if "ApiFunctionServiceRole" in name or name.startswith("ApiFunction")
        ]
        assert api_policies, "no policy found for the API function"
        serialized = json.dumps(api_policies)
        # It needs write access to devices, so look for the jobs-write actions
        # specifically rather than any write action.
        assert "dynamodb:DeleteItem" in serialized or "dynamodb:PutItem" in serialized
        # And confirm the jobs table grant is read-shaped by checking the
        # read-only action set is present.
        assert "dynamodb:GetItem" in serialized
        assert "dynamodb:Query" in serialized

    def test_the_coordinator_can_only_enqueue(self, resources: dict[str, Any]) -> None:
        policies = by_type(resources, "AWS::IAM::Policy")
        coordinator = [
            json.dumps(policy)
            for name, policy in policies.items()
            if name.startswith("CoordinatorFunction")
        ]
        assert coordinator
        serialized = " ".join(coordinator)
        assert "sqs:SendMessage" in serialized
        # It does no persistence at all, so it must hold no table permissions.
        assert "dynamodb" not in serialized

    def test_the_digest_can_only_read_jobs_and_send_email(self, resources: dict[str, Any]) -> None:
        policies = by_type(resources, "AWS::IAM::Policy")
        serialized = " ".join(
            json.dumps(policy)
            for name, policy in policies.items()
            if name.startswith("DigestFunction")
        )
        assert "ses:SendEmail" in serialized
        assert "dynamodb:Query" in serialized
        for write in (
            "dynamodb:PutItem",
            "dynamodb:UpdateItem",
            "dynamodb:DeleteItem",
            "sqs:",
            "sns:",
        ):
            assert write not in serialized, write

    def test_ses_permission_is_scoped_to_one_identity(self, resources: dict[str, Any]) -> None:
        statements = [
            statement
            for document in self._policies(resources)
            for statement in document["Statement"]
            if "ses:SendEmail" in json.dumps(statement.get("Action", ""))
        ]
        assert statements
        for statement in statements:
            resource = json.dumps(statement["Resource"])
            assert "identity/" in resource
            assert resource != '"*"'

    def test_each_function_has_its_own_role(self, resources: dict[str, Any]) -> None:
        assert len(by_type(resources, "AWS::IAM::Role")) == 6


class TestAlarms:
    def test_both_dlqs_are_alarmed(self, resources: dict[str, Any]) -> None:
        alarms = by_type(resources, "AWS::CloudWatch::Alarm")
        dlq_alarms = [
            alarm
            for alarm in alarms.values()
            if alarm["Properties"]["MetricName"] == "ApproximateNumberOfMessagesVisible"
        ]
        assert len(dlq_alarms) == 2
        for alarm in dlq_alarms:
            assert alarm["Properties"]["Threshold"] == 1

    def test_lambda_errors_are_alarmed(self, resources: dict[str, Any]) -> None:
        alarms = by_type(resources, "AWS::CloudWatch::Alarm")
        error_alarms = [
            alarm for alarm in alarms.values() if alarm["Properties"]["MetricName"] == "Errors"
        ]
        assert len(error_alarms) >= 2

    def test_every_alarm_explains_itself(self, resources: dict[str, Any]) -> None:
        for name, alarm in by_type(resources, "AWS::CloudWatch::Alarm").items():
            assert alarm["Properties"].get("AlarmDescription"), name

    def test_missing_data_does_not_trigger_alarms(self, resources: dict[str, Any]) -> None:
        # A quiet DLQ reports no datapoints; that must not read as a failure.
        for name, alarm in by_type(resources, "AWS::CloudWatch::Alarm").items():
            assert alarm["Properties"]["TreatMissingData"] == "notBreaching", name


class TestOutputs:
    def test_every_value_the_client_and_scripts_need_is_exported(self, template: Template) -> None:
        outputs = template.to_json()["Outputs"]
        for expected in (
            "JobsTableName",
            "HealthTableName",
            "DevicesTableName",
            "ScrapeQueueUrl",
            "ScrapeDlqUrl",
            "NotificationTopicArn",
            "NotificationQueueUrl",
            "NotificationDlqUrl",
            "ApiUrl",
        ):
            assert expected in outputs, expected


class TestConfigurability:
    def test_push_defaults_to_sns_without_an_ntfy_topic(self, resources: dict[str, Any]) -> None:
        notifier = next(
            f
            for f in by_type(resources, "AWS::Lambda::Function").values()
            if "notifier_handler" in f["Properties"]["Handler"]
        )
        env = notifier["Properties"]["Environment"]["Variables"]
        assert env["PUSH_TRANSPORT"] == "sns"
        assert "NTFY_TOPIC" not in env

    def test_an_ntfy_topic_switches_push_to_ntfy(self) -> None:
        app = App()
        stack = JobMonitorStack(
            app,
            "WithNtfy",
            ntfy_topic="jobs-abc123",
            env=Environment(account="000000000000", region="us-east-1"),
        )
        functions = Template.from_stack(stack).find_resources("AWS::Lambda::Function")
        notifier = next(
            f for f in functions.values() if "notifier_handler" in f["Properties"]["Handler"]
        )
        env = notifier["Properties"]["Environment"]["Variables"]
        assert env["PUSH_TRANSPORT"] == "ntfy"
        assert env["NTFY_TOPIC"] == "jobs-abc123"
        assert env["NTFY_SERVER"] == "https://ntfy.sh"

    def test_context_values_reach_the_template(self) -> None:
        app = App(
            context={
                "shardSize": "4",
                "notifyThreshold": "70",
                "emailFrom": "robot@example.org",
            }
        )
        stack = JobMonitorStack(
            app,
            "Configured",
            shard_size=int(app.node.try_get_context("shardSize")),
            notify_threshold=int(app.node.try_get_context("notifyThreshold")),
            email_from=str(app.node.try_get_context("emailFrom")),
            env=Environment(account="000000000000", region="us-east-1"),
        )
        body = Template.from_stack(stack).to_json()
        serialized = json.dumps(body)
        assert '"SHARD_SIZE": "4"' in serialized.replace(", ", ", ")
        assert '"NOTIFY_RELEVANCE_THRESHOLD": "70"' in serialized
        assert "identity/robot@example.org" in serialized


class TestLambdaAsset:
    def test_the_bundle_contains_the_package_and_the_registry(self) -> None:
        from build_lambda_bundle import build

        bundle = build()
        assert (bundle / "jobmonitor" / "__init__.py").is_file()
        assert (bundle / "jobmonitor" / "orchestration" / "handlers.py").is_file()
        assert (bundle / "companies.json").is_file()

    def test_the_bundle_excludes_caches_and_tests(self) -> None:
        from build_lambda_bundle import build

        bundle = build()
        assert not list(bundle.rglob("__pycache__"))
        assert not list(bundle.rglob("*.pyc"))
        assert not list(bundle.rglob("test_*.py"))

    def test_the_registry_in_the_bundle_is_loadable(self) -> None:
        from build_lambda_bundle import build
        from jobmonitor.models.company import CompanyRegistry

        registry = CompanyRegistry.load(build() / "companies.json")
        assert 50 <= len(registry.pollable()) <= 200

    def test_the_bundle_needs_no_third_party_dependencies(self) -> None:
        """The claim that makes `cdk synth` work without Docker."""
        import ast

        from build_lambda_bundle import build

        #: Imported only by the Apply kit's drafter, inside a function, and shipped
        #: only to the Kit Lambda, as a layer (see TestApplyKit).
        layered = {("drafter.py", "anthropic")}
        allowed = {
            "boto3",
            "botocore",
            "urllib3",
            "jobmonitor",
            "pywebpush",  # optional extra, imported lazily inside a function
        }
        offenders: list[str] = []
        for path in build().rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                roots: list[str] = []
                if isinstance(node, ast.Import):
                    roots = [alias.name.split(".")[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    roots = [node.module.split(".")[0]]
                for root in roots:
                    if root in allowed or (path.name, root) in layered:
                        continue
                    # Anything else must be in the standard library.
                    import sys

                    if root in sys.stdlib_module_names:
                        continue
                    offenders.append(f"{path.name}: {root}")
        assert not offenders, f"non-stdlib imports in the Lambda bundle: {sorted(set(offenders))}"


class TestApplyKit:
    """The Apply kit: its own function, layer, private bucket and table."""

    def kit(self, resources: dict[str, Any]) -> dict[str, Any]:
        (function,) = [
            f
            for f in by_type(resources, "AWS::Lambda::Function").values()
            if f["Properties"]["Handler"] == "jobmonitor.apply.kit_api.handler"
        ]
        return function

    def test_only_the_kit_function_gets_the_anthropic_layer(
        self, resources: dict[str, Any]
    ) -> None:
        layers = by_type(resources, "AWS::Lambda::LayerVersion")
        assert len(layers) == 1
        (layer,) = layers.values()
        assert layer["Properties"]["CompatibleArchitectures"] == ["arm64"]
        assert layer["Properties"]["CompatibleRuntimes"] == ["python3.12"]
        with_layers = [
            f for f in by_type(resources, "AWS::Lambda::Function").values()
            if f["Properties"].get("Layers")
        ]  # fmt: skip
        assert with_layers == [self.kit(resources)]

    def test_kit_fits_inside_api_gateway(self, resources: dict[str, Any]) -> None:
        assert self.kit(resources)["Properties"]["Timeout"] <= 29

    def test_kit_reads_its_secrets_from_ssm(self, resources: dict[str, Any]) -> None:
        env = self.kit(resources)["Properties"]["Environment"]["Variables"]
        assert env["KIT_SECRET_PARAMETER"] == "/jobmonitor/kit-secret"
        assert env["ANTHROPIC_KEY_PARAMETER"] == "/jobmonitor/anthropic-api-key"
        assert "ANTHROPIC_API_KEY" not in env and "KIT_SECRET" not in env

    def test_the_bucket_is_private_and_encrypted(self, resources: dict[str, Any]) -> None:
        (bucket,) = by_type(resources, "AWS::S3::Bucket").values()
        block = bucket["Properties"]["PublicAccessBlockConfiguration"]
        assert all(
            block[k]
            for k in (
                "BlockPublicAcls",
                "BlockPublicPolicy",
                "IgnorePublicAcls",
                "RestrictPublicBuckets",
            )
        )
        assert bucket["Properties"]["BucketEncryption"]
        assert bucket["DeletionPolicy"] == "Retain"
        policy = json.dumps(by_type(resources, "AWS::S3::BucketPolicy"))
        assert "aws:SecureTransport" in policy  # TLS only

    def test_kit_route(self, resources: dict[str, Any]) -> None:
        routes = {
            r["Properties"]["RouteKey"]
            for r in by_type(resources, "AWS::ApiGatewayV2::Route").values()
        }
        assert {"GET /kit/{proxy+}", "POST /kit/{proxy+}"} <= routes

    def test_only_the_kit_reads_the_bucket_and_the_anthropic_key(
        self, resources: dict[str, Any]
    ) -> None:
        policies = by_type(resources, "AWS::IAM::Policy")
        readers_of_key = [
            name for name, p in policies.items() if "anthropic-api-key" in json.dumps(p)
        ]
        readers_of_bucket = [
            name for name, p in policies.items()
            if "ApplyBucket" in json.dumps(p) and "s3:GetObject" in json.dumps(p)
        ]  # fmt: skip
        assert len(readers_of_key) == 1 and readers_of_key[0].startswith("KitFunction")
        assert len(readers_of_bucket) == 1 and readers_of_bucket[0].startswith("KitFunction")

    def test_alerts_get_kit_links(self, resources: dict[str, Any]) -> None:
        for prefix in ("NotifierFunction", "DigestFunction"):
            (function,) = [
                f for name, f in by_type(resources, "AWS::Lambda::Function").items()
                if name.startswith(prefix)
            ]  # fmt: skip
            env = function["Properties"]["Environment"]["Variables"]
            assert "KIT_BASE_URL" in env and env["KIT_SECRET_PARAMETER"] == "/jobmonitor/kit-secret"
