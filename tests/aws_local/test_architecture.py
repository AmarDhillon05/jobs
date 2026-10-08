"""Level 7 - the selected architecture, on emulated AWS (PRD §14 Level 7, Gate E).

Required scenario, and what is genuinely exercised here rather than mocked:

    scheduled event   -> injected (see "simulated boundary" below)
    -> coordinator       real Lambda, in a container
    -> SQS queue         real queue, real redrive policy
    -> worker Lambda     real SQS event source mapping triggers it
    -> DynamoDB          real tables, real GSIs
    -> SNS topic         real topic
    -> SQS queue         real SNS->SQS subscription
    -> notifier Lambda   real event source, real conditional write
    -> API/feed          real API Gateway -> real Lambda -> real DynamoDB, over HTTP

**Simulated boundary.** One thing is injected rather than emulated: the
EventBridge schedule. LocalStack's community scheduler emulation is unreliable, so
following PRD §18.4 the EventBridge-compatible rule stays in the CDK stack (and is
asserted in infrastructure/tests) while here the *identical event payload* is
delivered to the coordinator directly. Nothing downstream of the coordinator is
simulated.

**Deterministic sources.** Companies carried in these messages use the `fixture`
provider, which PRD §14 expressly permits ("may use deterministic scraper fixtures
while exercising real local AWS-emulated infrastructure"). Provider HTTP behaviour
is covered exhaustively at Level 2; `test_deployed_worker_reaches_the_network` here
separately proves the deployed worker really does make outbound calls.
"""

from __future__ import annotations

import json
import time
from typing import Any
from urllib.parse import quote

import pytest
from tests.aws_local.conftest import (
    Resources,
    fixture_company,
    http_get_json,
    intern_posting,
    wait_for,
)

from jobmonitor.orchestration.messages import ScheduledPollEvent, ScrapeTask

pytestmark = pytest.mark.aws_local


# --------------------------------------------------------------------- helpers


def invoke(aws: dict[str, Any], function: str, payload: Any) -> dict[str, Any]:
    response = aws["lambda"].invoke(FunctionName=function, Payload=json.dumps(payload).encode())
    body = response["Payload"].read().decode()
    assert response.get("FunctionError") is None, f"{function} errored: {body}"
    return json.loads(body) if body else {}


def publish_task(aws: dict[str, Any], resources: Resources, task: ScrapeTask) -> None:
    aws["sqs"].send_message(QueueUrl=resources.scrape_queue_url, MessageBody=task.to_json())


def scan_jobs(aws: dict[str, Any], resources: Resources) -> list[dict[str, Any]]:
    return aws["dynamodb"].scan(TableName=resources.jobs_table).get("Items", [])


def get_job(aws: dict[str, Any], resources: Resources, job_id: str) -> dict[str, Any] | None:
    item = aws["dynamodb"].get_item(TableName=resources.jobs_table, Key={"job_id": {"S": job_id}})
    return item.get("Item")


def scan_health(aws: dict[str, Any], resources: Resources) -> list[dict[str, Any]]:
    return aws["dynamodb"].scan(TableName=resources.health_table).get("Items", [])


def queue_depth(aws: dict[str, Any], url: str) -> int:
    attributes = aws["sqs"].get_queue_attributes(
        QueueUrl=url,
        AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
    )["Attributes"]
    return int(attributes["ApproximateNumberOfMessages"]) + int(
        attributes["ApproximateNumberOfMessagesNotVisible"]
    )


# ------------------------------------------------------------ the resources


class TestProvisionedResources:
    """The stack really is there, with the shape the code assumes."""

    def test_all_six_functions_exist_and_are_active(
        self, aws: dict[str, Any], resources: Resources
    ) -> None:
        for name in (
            "jobmonitor-coordinator",
            "jobmonitor-worker",
            "jobmonitor-notifier",
            "jobmonitor-digest",
            "jobmonitor-api",
            "jobmonitor-kit",
        ):
            config = aws["lambda"].get_function(FunctionName=name)["Configuration"]
            assert config["State"] == "Active", f"{name}: {config.get('StateReason')}"
            assert config["Handler"].startswith("jobmonitor.")

    def test_the_jobs_table_has_its_three_indexes(
        self, aws: dict[str, Any], resources: Resources
    ) -> None:
        from jobmonitor.storage.dynamo import COMPANY_INDEX, PENDING_INDEX, RECENT_INDEX

        table = aws["dynamodb"].describe_table(TableName=resources.jobs_table)["Table"]
        names = {index["IndexName"] for index in table.get("GlobalSecondaryIndexes", [])}
        assert names == {RECENT_INDEX, COMPANY_INDEX, PENDING_INDEX}

    def test_both_work_queues_dead_letter(self, aws: dict[str, Any], resources: Resources) -> None:
        for url in (resources.scrape_queue_url, resources.notification_queue_url):
            attributes = aws["sqs"].get_queue_attributes(
                QueueUrl=url, AttributeNames=["RedrivePolicy"]
            )["Attributes"]
            policy = json.loads(attributes["RedrivePolicy"])
            assert int(policy["maxReceiveCount"]) == 3
            assert policy["deadLetterTargetArn"]

    def test_the_topic_fans_out_to_the_notification_queue(
        self, aws: dict[str, Any], resources: Resources
    ) -> None:
        subscriptions = aws["sns"].list_subscriptions_by_topic(
            TopicArn=resources.notification_topic_arn
        )["Subscriptions"]
        assert any(s["Protocol"] == "sqs" for s in subscriptions)

    def test_the_event_source_mappings_report_batch_item_failures(
        self, aws: dict[str, Any], resources: Resources
    ) -> None:
        for function in ("jobmonitor-worker", "jobmonitor-notifier"):
            mappings = aws["lambda"].list_event_source_mappings(FunctionName=function)[
                "EventSourceMappings"
            ]
            assert mappings, f"{function} has no event source"
            assert mappings[0].get("FunctionResponseTypes") == ["ReportBatchItemFailures"]


# ------------------------------------------------------- schedule -> coordinator


class TestScheduledPollReachesTheQueue:
    def test_the_injected_schedule_event_shards_the_whole_registry(
        self, aws: dict[str, Any], resources: Resources, drain_queues: None
    ) -> None:
        """The payload is byte-identical to what the EventBridge rule sends."""
        event = ScheduledPollEvent(poll_id="level7-dry", dry_run=True).to_dict()
        result = invoke(aws, "jobmonitor-coordinator", {"detail": event})

        assert 50 <= result["companies"] <= 200
        assert result["shards"] == pytest.approx(result["companies"] / 8, abs=1)
        assert result["published"] == 0  # dry run

    def test_a_real_poll_publishes_one_message_per_shard(
        self, aws: dict[str, Any], resources: Resources, drain_queues: None
    ) -> None:
        result = invoke(
            aws,
            "jobmonitor-coordinator",
            {"detail": {"poll_id": "level7-publish", "only_provider": "rippling"}},
        )
        assert result["published"] == result["shards"] >= 1
        assert result["publish_failures"] == []

    def test_the_coordinator_reads_the_registry_from_its_bundle(
        self, aws: dict[str, Any], resources: Resources, drain_queues: None
    ) -> None:
        """Proves companies.json really is inside the deployment artifact."""
        result = invoke(
            aws, "jobmonitor-coordinator", {"detail": {"poll_id": "x", "dry_run": True}}
        )
        assert result["companies"] >= 50


# ------------------------------------------------------ the whole path (Gate F)


class TestScenario1NewJob:
    """PRD §30 Scenario 1, on emulated AWS."""

    def test_a_new_posting_traverses_the_entire_architecture(
        self, aws: dict[str, Any], resources: Resources, clean_tables: None, drain_queues: None
    ) -> None:
        task = ScrapeTask(
            poll_id="s1",
            shard_index=0,
            shard_count=1,
            companies=(fixture_company(jobs=[intern_posting("1")]),),
        )
        publish_task(aws, resources, task)

        items = wait_for(
            lambda: scan_jobs(aws, resources) or None,
            what="the worker Lambda to store the job in DynamoDB",
        )
        assert len(items) == 1
        stored = items[0]

        # Exactly one new record, with the application URL preserved.
        assert stored["job_id"]["S"] == "testco:1"
        assert stored["title"]["S"] == "Software Engineer Intern"
        assert stored["url"]["S"] == "https://example.test/jobs/1"
        assert stored["company"]["S"] == "TestCo"
        assert int(stored["relevance_score"]["N"]) >= 55
        assert stored["first_seen"]["S"]

        # The notification travelled SNS -> SQS -> notifier Lambda, which marked it.
        notified = wait_for(
            lambda: (
                (get_job(aws, resources, "testco:1") or {}).get("notification_sent", {}).get("BOOL")
                or None
            ),
            what="the notifier Lambda to flag the record as notified",
        )
        assert notified is True

        # And it is visible on the feed, over real HTTP.
        status, body = http_get_json(f"{resources.api_url}/jobs")
        assert status == 200
        assert body["count"] == 1
        assert body["jobs"][0]["job_id"] == "testco:1"

    def test_the_deep_link_target_resolves_through_the_api(
        self, aws: dict[str, Any], resources: Resources, clean_tables: None, drain_queues: None
    ) -> None:
        """The path a notification tap produces must resolve to that job."""
        publish_task(
            aws,
            resources,
            ScrapeTask(
                poll_id="s1-link",
                shard_index=0,
                shard_count=1,
                companies=(fixture_company(jobs=[intern_posting("1")]),),
            ),
        )
        wait_for(lambda: get_job(aws, resources, "testco:1"), what="the job to be stored")

        status, body = http_get_json(f"{resources.api_url}/jobs/{quote('testco:1', safe='')}")
        assert status == 200
        assert body["job"]["job_id"] == "testco:1"
        assert body["job"]["url"] == "https://example.test/jobs/1"

    def test_scraper_health_is_recorded(
        self, aws: dict[str, Any], resources: Resources, clean_tables: None, drain_queues: None
    ) -> None:
        publish_task(
            aws,
            resources,
            ScrapeTask(
                poll_id="s1-health",
                shard_index=0,
                shard_count=1,
                companies=(fixture_company(jobs=[intern_posting("1")]),),
            ),
        )
        health = wait_for(
            lambda: scan_health(aws, resources) or None, what="a health record to be written"
        )
        record = next(h for h in health if h["company"]["S"] == "TestCo")
        assert record["status"]["S"] == "success"
        assert int(record["jobs_found"]["N"]) == 1
        assert int(record["new_jobs"]["N"]) == 1


class TestHourlyDigest:
    """The digest Lambda reads the real recent-jobs index and emails (console)."""

    def test_the_hour_a_job_was_found_in_gets_one_digest_and_the_next_gets_none(
        self, aws: dict[str, Any], resources: Resources, clean_tables: None, drain_queues: None
    ) -> None:
        from datetime import datetime, timedelta

        publish_task(
            aws,
            resources,
            ScrapeTask(
                poll_id="digest",
                shard_index=0,
                shard_count=1,
                companies=(fixture_company(jobs=[intern_posting("1")]),),
            ),
        )
        [stored] = wait_for(
            lambda: scan_jobs(aws, resources) or None, what="the worker to store the job"
        )
        first_seen = datetime.fromisoformat(stored["first_seen"]["S"])
        hour = first_seen.replace(minute=0, second=0, microsecond=0)

        # The event EventBridge would deliver at :02 past the following hour.
        run = hour + timedelta(hours=1, minutes=2)
        summary = invoke(
            aws, "jobmonitor-digest", {"source": "aws.events", "time": run.isoformat()}
        )
        assert summary["sent"] is True
        assert summary["jobs"] == 1
        assert summary["window_start"] == hour.isoformat()

        quiet = invoke(
            aws,
            "jobmonitor-digest",
            {"source": "aws.events", "time": (run + timedelta(hours=1)).isoformat()},
        )
        assert quiet["sent"] is False and quiet["skipped_empty"] is True


class TestScenario2SamePollAgain:
    """PRD §30 Scenario 2: duplicate suppression, proven on emulated AWS."""

    def test_the_same_poll_creates_no_duplicate_and_sends_no_second_alert(
        self, aws: dict[str, Any], resources: Resources, clean_tables: None, drain_queues: None
    ) -> None:
        def task(poll_id: str) -> ScrapeTask:
            return ScrapeTask(
                poll_id=poll_id,
                shard_index=0,
                shard_count=1,
                companies=(fixture_company(jobs=[intern_posting("1")]),),
            )

        publish_task(aws, resources, task("s2-first"))
        wait_for(
            lambda: (
                (get_job(aws, resources, "testco:1") or {}).get("notification_sent", {}).get("BOOL")
                or None
            ),
            what="the first poll to complete and notify",
        )
        first = get_job(aws, resources, "testco:1")
        assert first is not None
        first_seen = first["first_seen"]["S"]
        notified_at = first["notified_at"]["S"]

        # Run the identical poll again.
        publish_task(aws, resources, task("s2-second"))
        wait_for(
            lambda: (
                int((get_job(aws, resources, "testco:1") or {})["times_seen"]["N"]) >= 2 or None
            ),
            what="the second poll to record another sighting",
        )
        # Give any (incorrect) second notification time to arrive.
        time.sleep(5)

        second = get_job(aws, resources, "testco:1")
        assert second is not None
        assert len(scan_jobs(aws, resources)) == 1, "a duplicate record was created"
        assert second["first_seen"]["S"] == first_seen, "first_seen moved"
        assert second["last_seen"]["S"] > first_seen, "last_seen did not advance"
        assert int(second["times_seen"]["N"]) >= 2
        # The authoritative duplicate-notification check: the timestamp is unchanged.
        assert second["notified_at"]["S"] == notified_at, "the job was notified twice"
        assert "notification_pending" not in second, "the pending marker came back"

    def test_the_feed_still_shows_exactly_one_job(
        self, aws: dict[str, Any], resources: Resources, clean_tables: None, drain_queues: None
    ) -> None:
        for poll in ("a", "b", "c"):
            publish_task(
                aws,
                resources,
                ScrapeTask(
                    poll_id=poll,
                    shard_index=0,
                    shard_count=1,
                    companies=(fixture_company(jobs=[intern_posting("1")]),),
                ),
            )
        wait_for(lambda: get_job(aws, resources, "testco:1"), what="the job to be stored")
        time.sleep(6)
        status, body = http_get_json(f"{resources.api_url}/jobs")
        assert status == 200
        assert body["count"] == 1


class TestScenario3Mixed:
    """PRD §30 Scenario 3: 10 existing + 1 new -> exactly one new alert."""

    def test_only_the_new_job_is_new_on_the_second_poll(
        self, aws: dict[str, Any], resources: Resources, clean_tables: None, drain_queues: None
    ) -> None:
        existing = [intern_posting(str(index)) for index in range(10)]
        publish_task(
            aws,
            resources,
            ScrapeTask(
                poll_id="s3-first",
                shard_index=0,
                shard_count=1,
                companies=(fixture_company(jobs=existing),),
            ),
        )
        wait_for(
            lambda: len(scan_jobs(aws, resources)) == 10 or None,
            what="all 10 jobs to be stored",
        )

        publish_task(
            aws,
            resources,
            ScrapeTask(
                poll_id="s3-second",
                shard_index=0,
                shard_count=1,
                companies=(fixture_company(jobs=[*existing, intern_posting("10")]),),
            ),
        )
        wait_for(
            lambda: len(scan_jobs(aws, resources)) == 11 or None,
            what="the 11th job to be stored",
        )

        health = scan_health(aws, resources)
        record = next(h for h in health if h["company"]["S"] == "TestCo")
        assert int(record["new_jobs"]["N"]) == 1, "the second poll should find exactly one new job"
        assert int(record["jobs_found"]["N"]) == 11


class TestScenario4ScraperFailureIsolation:
    """PRD §30 Scenario 4 / Gate F: one broken scraper, the others carry on."""

    def test_a_failing_company_does_not_stop_its_shard_mates(
        self, aws: dict[str, Any], resources: Resources, clean_tables: None, drain_queues: None
    ) -> None:
        task = ScrapeTask(
            poll_id="s4",
            shard_index=0,
            shard_count=1,
            companies=(
                fixture_company("Company A", fail="upstream returned an HTML error page"),
                fixture_company("Company B", jobs=[intern_posting("b1")]),
                fixture_company("Company C", jobs=[intern_posting("c1")]),
            ),
        )
        publish_task(aws, resources, task)

        wait_for(
            lambda: len(scan_jobs(aws, resources)) == 2 or None,
            what="Company B and Company C to be stored despite Company A failing",
        )
        companies = {item["company"]["S"] for item in scan_jobs(aws, resources)}
        assert companies == {"Company B", "Company C"}

        health = {h["company"]["S"]: h for h in scan_health(aws, resources)}
        assert health["Company A"]["status"]["S"] == "failed"
        assert "ParseError" in health["Company A"]["error_type"]["S"]
        assert health["Company B"]["status"]["S"] == "success"
        assert health["Company C"]["status"]["S"] == "success"

    def test_a_broken_scraper_does_not_dead_letter_the_shard(
        self, aws: dict[str, Any], resources: Resources, clean_tables: None, drain_queues: None
    ) -> None:
        """Retrying someone else's outage is useless, so the message must succeed."""
        publish_task(
            aws,
            resources,
            ScrapeTask(
                poll_id="s4-dlq",
                shard_index=0,
                shard_count=1,
                companies=(fixture_company("Company A", fail="site is down"),),
            ),
        )
        wait_for(lambda: scan_health(aws, resources) or None, what="the failure to be recorded")
        time.sleep(8)
        assert queue_depth(aws, resources.scrape_dlq_url) == 0


class TestScenario7QueueFailureAndDlq:
    """PRD §30 Scenario 7 / Level 6 case 10: permanent failure reaches the DLQ."""

    def test_an_undecodable_message_is_dead_lettered_with_its_body_intact(
        self, aws: dict[str, Any], resources: Resources, clean_tables: None, drain_queues: None
    ) -> None:
        """One test, not two: each one costs maxReceiveCount x visibility timeout
        of real waiting, so depth and body are asserted together."""
        poison = "this is not a scrape task"
        aws["sqs"].send_message(QueueUrl=resources.scrape_queue_url, MessageBody=poison)

        wait_for(
            lambda: queue_depth(aws, resources.scrape_dlq_url) or None,
            what=f"the poison message to be dead-lettered after {3} attempts",
            # 3 receives x the queue's visibility timeout, plus slack.
            timeout=420.0,
        )
        received = aws["sqs"].receive_message(
            QueueUrl=resources.scrape_dlq_url, MaxNumberOfMessages=1, WaitTimeSeconds=5
        )
        assert received.get("Messages"), "the DLQ reported depth but returned nothing"
        assert received["Messages"][0]["Body"] == poison, "the body was not preserved"


# ------------------------------------------------------------------ the feed


class TestFeedPath:
    def test_the_api_serves_the_index_over_http(self, resources: Resources) -> None:
        status, body = http_get_json(f"{resources.api_url}/")
        assert status == 200
        assert "GET /jobs" in body["endpoints"]

    def test_the_api_reports_registry_coverage(self, resources: Resources) -> None:
        status, body = http_get_json(f"{resources.api_url}/meta")
        assert status == 200
        assert 50 <= body["companies"]["pollable"] <= 200

    def test_the_health_view_is_served(
        self, aws: dict[str, Any], resources: Resources, clean_tables: None, drain_queues: None
    ) -> None:
        publish_task(
            aws,
            resources,
            ScrapeTask(
                poll_id="feed-health",
                shard_index=0,
                shard_count=1,
                companies=(
                    fixture_company("Good Co", jobs=[intern_posting("1")]),
                    fixture_company("Bad Co", fail="boom"),
                ),
            ),
        )
        wait_for(
            lambda: len(scan_health(aws, resources)) == 2 or None,
            what="both health records to be written",
        )
        status, body = http_get_json(f"{resources.api_url}/health")
        assert status == 200
        assert body["scrapers_total"] == 2
        assert body["scrapers_failing"] == 1

    def test_an_unknown_job_is_404_over_http(self, resources: Resources) -> None:
        status, _body = http_get_json(f"{resources.api_url}/jobs/does-not-exist")
        assert status == 404

    def test_the_api_lambda_also_accepts_a_v2_proxy_event(
        self, aws: dict[str, Any], resources: Resources
    ) -> None:
        """The deployed stack fronts this Lambda with an HTTP (v2) API.

        LocalStack community only emulates REST (v1), so the v2 payload shape is
        covered by invoking the function with one directly - the same handler, the
        same translation code.
        """
        event = {
            "version": "2.0",
            "rawPath": "/meta",
            "requestContext": {"http": {"method": "GET", "path": "/meta"}, "stage": "$default"},
        }
        result = invoke(aws, "jobmonitor-api", event)
        assert result["statusCode"] == 200
        assert json.loads(result["body"])["companies"]["pollable"] >= 50


# ------------------------------------------------------------- the Apply kit


def kit_event(method: str, path: str, token: str, body: Any = None) -> dict[str, Any]:
    """An HTTP API (v2) event, as the deployed stack's /kit/{proxy+} route sends."""
    return {
        "version": "2.0",
        "rawPath": path,
        "requestContext": {"http": {"method": method, "path": path}, "stage": "$default"},
        "queryStringParameters": {"t": token},
        "body": json.dumps(body) if body is not None else None,
    }


class TestApplyKit:
    """A stored job's Apply kit, served by the real Kit Lambda on emulated AWS.

    The kit reads the profile from the LocalStack bucket and keeps answers in the
    LocalStack table. Drafts use the sample drafter here: there is no Anthropic key
    in LocalStack, and the deployed layer is built for ARM while LocalStack's
    runtime is x86 (ARCHITECTURE.md, local emulation gaps). The drafter itself is
    covered by request-shape tests.
    """

    def test_kit_page_draft_save_and_resume(
        self, aws: dict[str, Any], resources: Resources, clean_tables: None, drain_queues: None
    ) -> None:
        from jobmonitor.apply import tokens

        publish_task(
            aws,
            resources,
            ScrapeTask(
                poll_id="kit",
                shard_index=0,
                shard_count=1,
                companies=(fixture_company(jobs=[intern_posting("1")]),),
            ),
        )
        wait_for(lambda: get_job(aws, resources, "testco:1"), what="the job to be stored")
        token = tokens.sign("testco:1", "local-kit-secret")
        path = f"/kit/{quote('testco:1', safe='')}"

        page = invoke(aws, "jobmonitor-kit", kit_event("GET", path, token))
        assert page["statusCode"] == 200
        assert page["headers"]["Content-Type"].startswith("text/html")
        assert "alex.rivera@example.edu" in page["body"]  # profile read from S3
        assert "Common questions" in page["body"]

        drafted = invoke(
            aws,
            "jobmonitor-kit",
            kit_event("POST", f"{path}/draft", token, {"question": "Why TestCo?"}),
        )
        assert drafted["statusCode"] == 200
        assert json.loads(drafted["body"])["answer"]["source"] == "draft"

        saved = invoke(
            aws,
            "jobmonitor-kit",
            kit_event(
                "POST",
                f"{path}/answers",
                token,
                {"question": "Why TestCo?", "answer": "My words.", "save_to_library": True},
            ),
        )
        assert saved["statusCode"] == 200
        items = aws["dynamodb"].scan(TableName=resources.apply_table)["Items"]
        keys = {(item["pk"]["S"], item["sk"]["S"].split("#")[0]) for item in items}
        assert ("JOB#testco:1", "Q") in keys and ("LIBRARY", "Q") in keys
        assert any(item.get("text", {}).get("S") == "My words." for item in items)

        resume = invoke(aws, "jobmonitor-kit", kit_event("GET", f"{path}/resume", token))
        assert resume["statusCode"] == 302
        assert "resume.pdf" in resume["headers"]["Location"]

        refused = invoke(aws, "jobmonitor-kit", kit_event("GET", path, "wrong-token"))
        assert refused["statusCode"] == 404


# ----------------------------------------------------- the real outbound path


class TestDeployedWorkerNetworking:
    def test_deployed_worker_reaches_the_network(
        self, aws: dict[str, Any], resources: Resources, clean_tables: None, drain_queues: None
    ) -> None:
        """The deployed worker really does make outbound HTTP calls.

        The rest of this file uses deterministic fixtures, so this test exists to
        close the obvious gap: it polls a real registry company and asserts the
        worker got far enough to produce a health record naming the provider's real
        endpoint. It deliberately asserts *nothing* about success - PRD §14 Level 7
        says completion must not depend on an external site being online, and in
        this sandbox the egress policy blocks ATS hosts outright (BLOCKERS.md
        BLK-001). Either way it proves the HTTP path is wired, not stubbed.
        """
        invoke(
            aws,
            "jobmonitor-coordinator",
            {"detail": {"poll_id": "outbound", "only_companies": ["Anthropic"]}},
        )
        health = wait_for(
            lambda: (
                [h for h in scan_health(aws, resources) if h["company"]["S"] == "Anthropic"] or None
            ),
            what="a health record from a real outbound poll",
            timeout=150.0,
        )
        record = health[0]
        status = record["status"]["S"]
        assert status in {"success", "empty", "degraded", "failed", "blocked"}
        if status in {"failed", "blocked"}:
            # Whatever happened, it happened while talking to the real endpoint.
            assert "greenhouse.io" in record["error"]["S"]
