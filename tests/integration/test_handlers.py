"""Level 6 - the Lambda handlers, including the queue retry/DLQ contract.

These tests drive the handlers with the event shapes AWS actually delivers
(EventBridge envelopes, SQS record batches, SNS-inside-SQS envelopes) and assert
the responses AWS actually consumes - notably ``batchItemFailures``, which is what
makes SQS retry only the failed shard and eventually route it to the DLQ instead of
replaying the healthy ones (PRD §14 L6 case 10, §30 Scenario 7).

Real SQS/SNS behaviour under Moto is covered here too, so the DLQ redrive is
observed rather than assumed.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from typing import Any, ClassVar

import pytest
from tests.conftest import make_company

from jobmonitor.config import AwsSettings, Settings, for_tests
from jobmonitor.models.company import Company, CompanyRegistry
from jobmonitor.models.job import Job
from jobmonitor.notifications import (
    MemoryEmailTransport,
    MemoryPushTransport,
    Notifier,
)
from jobmonitor.notifications.events import NotificationEvent
from jobmonitor.orchestration.handlers import (
    Dependencies,
    coordinator_handler,
    notifier_handler,
    worker_handler,
)
from jobmonitor.orchestration.messages import ScheduledPollEvent, ScrapeTask
from jobmonitor.orchestration.queues import InMemoryQueue, InMemoryTopic, PublishError
from jobmonitor.scrapers.base import JobSource
from jobmonitor.storage.memory import (
    InMemoryDeviceRepository,
    InMemoryHealthRepository,
    InMemoryJobRepository,
)

pytestmark = pytest.mark.integration

T0 = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


class StaticSource(JobSource):
    provider: ClassVar[str] = "static"
    required_config: ClassVar[tuple[str, ...]] = ()

    def __init__(self, company: Company, jobs: Sequence[Job], **kwargs: Any) -> None:
        super().__init__(company, **kwargs)
        self._jobs = list(jobs)

    def fetch_pages(self) -> Iterator[Sequence[Any]]:
        yield self._jobs

    def normalize(self, raw_job: Any) -> Job:
        return raw_job


def intern(identifier: str, company: str = "TestCo") -> Job:
    return Job(
        company=company,
        title="Software Engineer Intern",
        url=f"https://job-boards.greenhouse.io/testco/jobs/{identifier}",
        source="greenhouse",
        external_id=identifier,
        location="San Francisco, CA",
    )


def build_deps(
    companies: Sequence[Company] | None = None,
    *,
    settings: Settings | None = None,
    queue: InMemoryQueue | None = None,
    topic: InMemoryTopic | None = None,
    repository: InMemoryJobRepository | None = None,
    email: MemoryEmailTransport | None = None,
    push: MemoryPushTransport | None = None,
) -> Dependencies:
    """Dependencies with every AWS edge replaced by an in-memory double."""
    settings = settings or for_tests(app_base_url="https://app.test", shard_size=2)
    repository = repository or InMemoryJobRepository()
    email = email or MemoryEmailTransport()
    push = push or MemoryPushTransport()

    class TestDependencies(Dependencies):
        def notifier(self) -> Notifier:
            return Notifier(
                self.settings,
                repository=self.repository,
                email_transport=email,
                push_transport=push,
                device_repository=self.devices,
            )

    return TestDependencies(
        settings=settings,
        registry=CompanyRegistry(tuple(companies if companies is not None else (make_company(),))),
        repository=repository,
        health=InMemoryHealthRepository(),
        devices=InMemoryDeviceRepository(),
        scrape_queue=queue or InMemoryQueue(),
        notification_topic=topic or InMemoryTopic(),
    )


# ------------------------------------------------------------------ coordinator


class TestCoordinator:
    def test_shards_the_registry_and_publishes_one_message_per_shard(self) -> None:
        queue = InMemoryQueue()
        companies = [make_company(f"Co {index}") for index in range(5)]
        deps = build_deps(companies, queue=queue)

        result = coordinator_handler({"poll_id": "p1"}, deps=deps)

        assert result["companies"] == 5
        assert result["shards"] == 3  # shard_size=2 -> 2+2+1
        assert result["published"] == 3
        assert len(queue.bodies) == 3

    def test_each_message_is_a_decodable_scrape_task(self) -> None:
        queue = InMemoryQueue()
        companies = [make_company(f"Co {index}") for index in range(3)]
        deps = build_deps(companies, queue=queue)
        coordinator_handler({"poll_id": "p1"}, deps=deps)

        tasks = [ScrapeTask.from_json(body) for body in queue.bodies]
        assert sum(len(task.companies) for task in tasks) == 3
        assert {task.poll_id for task in tasks} == {"p1"}
        assert {task.shard_count for task in tasks} == {2}
        # The full company config travels with the message.
        assert tasks[0].companies[0].provider_config

    def test_accepts_a_raw_eventbridge_envelope(self) -> None:
        queue = InMemoryQueue()
        deps = build_deps(queue=queue)
        event = {
            "version": "0",
            "id": "eb-1234",
            "detail-type": "Scheduled Event",
            "source": "aws.scheduler",
            "time": "2026-09-26T12:00:00Z",
            "detail": {"poll_id": "scheduled-1"},
        }
        result = coordinator_handler(event, deps=deps)
        assert result["poll_id"] == "scheduled-1"
        assert result["published"] == 1

    def test_falls_back_to_the_event_id_when_no_poll_id_is_given(self) -> None:
        deps = build_deps(queue=InMemoryQueue())
        result = coordinator_handler({"id": "eb-9", "time": "2026-09-26T12:00:00Z"}, deps=deps)
        assert result["poll_id"] == "eb-9"

    def test_a_dry_run_publishes_nothing(self) -> None:
        queue = InMemoryQueue()
        deps = build_deps(queue=queue)
        result = coordinator_handler({"poll_id": "p1", "dry_run": True}, deps=deps)
        assert result["published"] == 0
        assert queue.bodies == []

    def test_can_be_narrowed_to_specific_companies(self) -> None:
        queue = InMemoryQueue()
        companies = [make_company("Alpha"), make_company("Beta"), make_company("Gamma")]
        deps = build_deps(companies, queue=queue)
        coordinator_handler({"poll_id": "p1", "only_companies": ["Beta"]}, deps=deps)
        task = ScrapeTask.from_json(queue.bodies[0])
        assert task.company_names == ("Beta",)

    def test_can_be_narrowed_to_one_provider(self) -> None:
        queue = InMemoryQueue()
        companies = [
            make_company("Alpha", provider="greenhouse"),
            make_company("Beta", provider="lever"),
        ]
        deps = build_deps(companies, queue=queue)
        coordinator_handler({"poll_id": "p1", "only_provider": "lever"}, deps=deps)
        assert ScrapeTask.from_json(queue.bodies[0]).company_names == ("Beta",)

    def test_a_publish_failure_is_loud_because_that_shard_would_never_run(self) -> None:
        queue = InMemoryQueue(fail_indices=frozenset({1}))
        companies = [make_company(f"Co {index}") for index in range(4)]
        deps = build_deps(companies, queue=queue)
        with pytest.raises(PublishError, match="failed to publish"):
            coordinator_handler({"poll_id": "p1"}, deps=deps)

    def test_an_empty_registry_publishes_nothing_without_failing(self) -> None:
        deps = build_deps([], queue=InMemoryQueue())
        result = coordinator_handler({"poll_id": "p1"}, deps=deps)
        assert result["shards"] == 0
        assert result["published"] == 0

    def test_unsupported_companies_are_not_shipped_to_workers(self) -> None:
        from jobmonitor.models.company import SupportStatus

        queue = InMemoryQueue()
        companies = [
            make_company("Good Co"),
            make_company("Blocked Co", support_status=SupportStatus.BLOCKED),
        ]
        deps = build_deps(companies, queue=queue)
        coordinator_handler({"poll_id": "p1"}, deps=deps)
        assert ScrapeTask.from_json(queue.bodies[0]).company_names == ("Good Co",)


# ----------------------------------------------------------------------- worker


def sqs_event(*bodies: str, prefix: str = "msg") -> dict[str, Any]:
    return {
        "Records": [
            {
                "messageId": f"{prefix}-{index}",
                "receiptHandle": f"handle-{index}",
                "body": body,
                "attributes": {"ApproximateReceiveCount": "1"},
                "eventSource": "aws:sqs",
            }
            for index, body in enumerate(bodies)
        ]
    }


class TestWorker:
    def _task_body(self, *companies: Company, poll_id: str = "p1") -> str:
        return ScrapeTask(
            poll_id=poll_id, shard_index=0, shard_count=1, companies=tuple(companies)
        ).to_json()

    def test_processes_a_shard_and_stores_the_jobs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        repository = InMemoryJobRepository()
        topic = InMemoryTopic()
        company = make_company()
        deps = build_deps([company], repository=repository, topic=topic)

        monkeypatch.setattr(
            "jobmonitor.orchestration.pipeline.build_source",
            lambda c, client=None: StaticSource(c, [intern("1")]),
        )
        result = worker_handler(sqs_event(self._task_body(company)), deps=deps)

        assert result["companies_processed"] == 1
        assert result["new_jobs"] == 1
        assert result["batchItemFailures"] == []
        assert repository.count() == 1

    def test_publishes_a_notification_event_for_new_jobs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        topic = InMemoryTopic()
        company = make_company()
        deps = build_deps([company], topic=topic)
        monkeypatch.setattr(
            "jobmonitor.orchestration.pipeline.build_source",
            lambda c, client=None: StaticSource(c, [intern("1")]),
        )
        worker_handler(sqs_event(self._task_body(company)), deps=deps)

        assert len(topic.messages) == 1
        event = NotificationEvent.from_json(topic.bodies()[0])
        assert len(event.jobs) == 1
        assert event.jobs[0].deep_link.startswith("https://app.test/jobs/")

    def test_publishes_nothing_when_there_is_nothing_new(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        topic = InMemoryTopic()
        repository = InMemoryJobRepository()
        company = make_company()
        deps = build_deps([company], topic=topic, repository=repository)
        monkeypatch.setattr(
            "jobmonitor.orchestration.pipeline.build_source",
            lambda c, client=None: StaticSource(c, [intern("1")]),
        )
        worker_handler(sqs_event(self._task_body(company)), deps=deps)
        topic.clear()

        worker_handler(sqs_event(self._task_body(company)), deps=deps)
        assert topic.messages == []

    def test_an_undecodable_message_is_reported_as_failed_not_retried_forever(self) -> None:
        deps = build_deps()
        result = worker_handler(sqs_event("this is not json"), deps=deps)
        # Reported as failed so SQS eventually DLQs it, rather than us looping.
        assert result["batchItemFailures"] == [{"itemIdentifier": "msg-0"}]

    def test_a_message_with_no_companies_is_reported_as_failed(self) -> None:
        deps = build_deps()
        result = worker_handler(sqs_event(json.dumps({"poll_id": "p", "companies": []})), deps=deps)
        assert result["batchItemFailures"] == [{"itemIdentifier": "msg-0"}]

    def test_only_the_failing_message_is_reported_in_a_mixed_batch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The core partial-batch guarantee: healthy shards are not replayed."""
        company = make_company()
        deps = build_deps([company])
        monkeypatch.setattr(
            "jobmonitor.orchestration.pipeline.build_source",
            lambda c, client=None: StaticSource(c, [intern("1")]),
        )
        result = worker_handler(sqs_event("not json", self._task_body(company)), deps=deps)
        assert result["batchItemFailures"] == [{"itemIdentifier": "msg-0"}]
        assert result["companies_processed"] == 1

    def test_a_storage_failure_reports_the_message_for_retry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class BrokenRepository(InMemoryJobRepository):
            def upsert(self, *args: Any, **kwargs: Any) -> Any:  # type: ignore[override]
                raise RuntimeError("DynamoDB unavailable")

        company = make_company()
        deps = build_deps([company], repository=BrokenRepository())
        monkeypatch.setattr(
            "jobmonitor.orchestration.pipeline.build_source",
            lambda c, client=None: StaticSource(c, [intern("1")]),
        )
        result = worker_handler(sqs_event(self._task_body(company)), deps=deps)
        assert result["batchItemFailures"] == [{"itemIdentifier": "msg-0"}]

    def test_a_failed_topic_publish_reports_the_message_for_retry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        topic = InMemoryTopic(fail_next=True)
        repository = InMemoryJobRepository()
        company = make_company()
        deps = build_deps([company], topic=topic, repository=repository)
        monkeypatch.setattr(
            "jobmonitor.orchestration.pipeline.build_source",
            lambda c, client=None: StaticSource(c, [intern("1")]),
        )
        result = worker_handler(sqs_event(self._task_body(company)), deps=deps)

        assert result["batchItemFailures"] == [{"itemIdentifier": "msg-0"}]
        # The job is stored and still pending, so the alert is not lost.
        assert repository.count() == 1
        assert repository.pending_notifications()

    def test_a_broken_scraper_does_not_fail_the_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A dead careers site is not a reason to retry the shard forever."""
        from jobmonitor.errors import ParseError

        class Broken(StaticSource):
            def fetch_pages(self) -> Iterator[Sequence[Any]]:
                raise ParseError("site is down")

        good = make_company("Good Co")
        bad = make_company("Bad Co")
        deps = build_deps([good, bad])

        def factory(company: Company, client: Any = None) -> JobSource:
            if company.company == "Bad Co":
                return Broken(company, [])
            return StaticSource(company, [intern("1", company="Good Co")])

        monkeypatch.setattr("jobmonitor.orchestration.pipeline.build_source", factory)
        result = worker_handler(sqs_event(self._task_body(good, bad)), deps=deps)

        assert result["batchItemFailures"] == []
        assert result["scrapers_failed"] == 1
        assert result["companies_processed"] == 2

    def test_an_empty_event_is_handled(self) -> None:
        assert worker_handler({}, deps=build_deps())["companies_processed"] == 0


# --------------------------------------------------------------------- notifier


class TestNotifierHandler:
    def _stored(self, repository: InMemoryJobRepository, count: int = 1) -> list[str]:
        ids = []
        for index in range(count):
            result = repository.upsert(intern(str(index)), relevance_score=73, now=T0)
            ids.append(result.record.job_id)
        return ids

    def _payload(self, repository: InMemoryJobRepository, job_ids: Sequence[str]) -> str:
        records = [repository.get(job_id) for job_id in job_ids]
        return NotificationEvent.for_records(
            [r for r in records if r], app_base_url="https://app.test"
        ).to_json()

    def test_delivers_email_and_push(self) -> None:
        repository = InMemoryJobRepository()
        email, push = MemoryEmailTransport(), MemoryPushTransport()
        deps = build_deps(repository=repository, email=email, push=push)
        job_ids = self._stored(repository)

        result = notifier_handler(sqs_event(self._payload(repository, job_ids)), deps=deps)

        assert result["notifications_emitted"] == 2
        assert len(email.sent) == 1
        assert len(push.sent) == 1
        assert result["batchItemFailures"] == []

    def test_unwraps_an_sns_inside_sqs_envelope(self) -> None:
        repository = InMemoryJobRepository()
        email = MemoryEmailTransport()
        deps = build_deps(repository=repository, email=email)
        job_ids = self._stored(repository)
        sns_envelope = json.dumps(
            {
                "Type": "Notification",
                "MessageId": "sns-1",
                "TopicArn": "arn:aws:sns:us-east-1:000000000000:alerts",
                "Subject": "New internships",
                "Message": self._payload(repository, job_ids),
            }
        )
        result = notifier_handler(sqs_event(sns_envelope), deps=deps)
        assert result["notifications_emitted"] == 2
        assert len(email.sent) == 1

    def test_a_redelivered_message_does_not_alert_twice(self) -> None:
        repository = InMemoryJobRepository()
        email = MemoryEmailTransport()
        deps = build_deps(repository=repository, email=email)
        job_ids = self._stored(repository)
        payload = self._payload(repository, job_ids)

        notifier_handler(sqs_event(payload), deps=deps)
        second = notifier_handler(sqs_event(payload), deps=deps)

        assert len(email.sent) == 1
        assert second["suppressed_duplicates"] == 1
        assert second["notifications_emitted"] == 0
        assert second["batchItemFailures"] == []

    def test_an_undecodable_payload_is_reported_for_the_dlq(self) -> None:
        deps = build_deps()
        result = notifier_handler(sqs_event("{}"), deps=deps)
        assert result["batchItemFailures"] == [{"itemIdentifier": "msg-0"}]

    def test_a_payload_for_jobs_that_no_longer_exist_is_dropped_quietly(self) -> None:
        repository = InMemoryJobRepository()
        deps = build_deps(repository=repository)
        payload = json.dumps(
            {
                "schema_version": 1,
                "urgency": "batched",
                "jobs": [
                    {
                        "job_id": "ghost:1",
                        "company": "X",
                        "title": "Intern",
                        "url": "https://x.example.com/1",
                        "deep_link": "https://app.test/jobs/ghost%3A1",
                    }
                ],
            }
        )
        result = notifier_handler(sqs_event(payload), deps=deps)
        # Nothing to send, and nothing to retry: the record is genuinely gone.
        assert result["notifications_emitted"] == 0
        assert result["batchItemFailures"] == []

    def test_total_delivery_failure_reports_the_message_for_retry(self) -> None:
        repository = InMemoryJobRepository()
        email = MemoryEmailTransport(fail_on_call=1)
        push = MemoryPushTransport(fail_on_call=1)
        deps = build_deps(repository=repository, email=email, push=push)
        job_ids = self._stored(repository)

        result = notifier_handler(sqs_event(self._payload(repository, job_ids)), deps=deps)

        assert result["notifications_failed"] == 2
        assert result["batchItemFailures"] == [{"itemIdentifier": "msg-0"}]
        assert repository.pending_notifications()

    def test_partial_delivery_success_is_not_retried(self) -> None:
        repository = InMemoryJobRepository()
        email = MemoryEmailTransport(fail_on_call=1)
        push = MemoryPushTransport()
        deps = build_deps(repository=repository, email=email, push=push)
        job_ids = self._stored(repository)

        result = notifier_handler(sqs_event(self._payload(repository, job_ids)), deps=deps)

        assert result["batchItemFailures"] == []
        assert len(push.sent) == 1


# ------------------------------------------------------- real SQS/SNS behaviour


class TestQueueBehaviourUnderMoto:
    """PRD §13.1: real queue semantics, including the DLQ redrive."""

    @pytest.fixture
    def sqs(self) -> Iterator[Any]:
        import boto3
        from moto import mock_aws

        with mock_aws():
            yield boto3.client("sqs", region_name="us-east-1")

    def test_batched_publishing_respects_the_ten_message_api_limit(self, sqs: Any) -> None:
        from jobmonitor.orchestration.queues import SqsQueue

        url = sqs.create_queue(QueueName="scrape")["QueueUrl"]
        queue = SqsQueue(url, client=sqs)

        result = queue.publish([json.dumps({"n": index}) for index in range(25)])

        assert result.ok
        assert result.count == 25
        attributes = sqs.get_queue_attributes(
            QueueUrl=url, AttributeNames=["ApproximateNumberOfMessages"]
        )
        assert int(attributes["Attributes"]["ApproximateNumberOfMessages"]) == 25

    def test_publishing_without_a_queue_url_is_a_clear_error(self) -> None:
        from jobmonitor.orchestration.queues import SqsQueue

        with pytest.raises(PublishError, match="SCRAPE_QUEUE_URL"):
            SqsQueue(None).publish(["{}"])

    def test_a_message_that_keeps_failing_lands_in_the_dlq(self, sqs: Any) -> None:
        """Scenario 7 / L6 case 10, observed rather than assumed."""
        dlq_url = sqs.create_queue(QueueName="scrape-dlq")["QueueUrl"]
        dlq_arn = sqs.get_queue_attributes(QueueUrl=dlq_url, AttributeNames=["QueueArn"])[
            "Attributes"
        ]["QueueArn"]
        queue_url = sqs.create_queue(
            QueueName="scrape",
            Attributes={
                "RedrivePolicy": json.dumps(
                    {"deadLetterTargetArn": dlq_arn, "maxReceiveCount": "3"}
                ),
                "VisibilityTimeout": "0",
            },
        )["QueueUrl"]

        sqs.send_message(QueueUrl=queue_url, MessageBody="poison")
        # Receive without deleting: the worker "failed" each time.
        for _ in range(4):
            sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=1)

        dlq_messages = sqs.receive_message(QueueUrl=dlq_url, MaxNumberOfMessages=1)
        assert dlq_messages.get("Messages"), "message never reached the DLQ"
        assert dlq_messages["Messages"][0]["Body"] == "poison"

    def test_a_deleted_message_is_not_redelivered(self, sqs: Any) -> None:
        url = sqs.create_queue(QueueName="scrape", Attributes={"VisibilityTimeout": "0"})[
            "QueueUrl"
        ]
        sqs.send_message(QueueUrl=url, MessageBody="work")
        received = sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=1)
        sqs.delete_message(QueueUrl=url, ReceiptHandle=received["Messages"][0]["ReceiptHandle"])
        assert not sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=1).get("Messages")


class TestTopicBehaviourUnderMoto:
    def test_sns_to_sqs_delivers_the_payload(self) -> None:
        import boto3
        from moto import mock_aws

        from jobmonitor.orchestration.queues import SnsTopic

        with mock_aws():
            sns = boto3.client("sns", region_name="us-east-1")
            sqs = boto3.client("sqs", region_name="us-east-1")
            topic_arn = sns.create_topic(Name="alerts")["TopicArn"]
            queue_url = sqs.create_queue(QueueName="notify")["QueueUrl"]
            queue_arn = sqs.get_queue_attributes(QueueUrl=queue_url, AttributeNames=["QueueArn"])[
                "Attributes"
            ]["QueueArn"]
            sns.subscribe(TopicArn=topic_arn, Protocol="sqs", Endpoint=queue_arn)

            SnsTopic(topic_arn, client=sns).publish('{"jobs": []}', subject="New internships")

            messages = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=1)
            assert messages.get("Messages")
            body = json.loads(messages["Messages"][0]["Body"])
            assert json.loads(body["Message"]) == {"jobs": []}

    def test_publishing_without_a_topic_arn_is_a_clear_error(self) -> None:
        from jobmonitor.orchestration.queues import SnsTopic

        with pytest.raises(PublishError, match="NOTIFICATION_TOPIC_ARN"):
            SnsTopic(None).publish("{}")


class TestMessageSchemas:
    def test_scrape_task_round_trips(self) -> None:
        task = ScrapeTask(
            poll_id="p1",
            shard_index=2,
            shard_count=5,
            companies=(make_company("Alpha"), make_company("Beta")),
        )
        restored = ScrapeTask.from_json(task.to_json())
        assert restored.company_names == ("Alpha", "Beta")
        assert restored.shard_index == 2
        assert restored.companies[0].provider_config == task.companies[0].provider_config

    def test_a_task_must_carry_companies(self) -> None:
        from jobmonitor.orchestration.messages import MessageError

        with pytest.raises(MessageError, match="at least one company"):
            ScrapeTask(poll_id="p", shard_index=0, shard_count=1, companies=())

    def test_an_unusable_company_in_a_task_is_a_message_error(self) -> None:
        from jobmonitor.orchestration.messages import MessageError

        with pytest.raises(MessageError, match="unusable company"):
            ScrapeTask.from_dict({"companies": [{"company": "X"}]})

    def test_scheduled_event_defaults_are_safe(self) -> None:
        event = ScheduledPollEvent.from_dict(None)
        assert event.poll_id
        assert event.dry_run is False
        assert event.only_companies == ()

    def test_sharding_covers_every_pollable_company_exactly_once(self) -> None:
        registry = CompanyRegistry(tuple(make_company(f"Co {i}") for i in range(17)))
        tasks = ScrapeTask.shard(registry, poll_id="p1", size=5)
        assert [len(task.companies) for task in tasks] == [5, 5, 5, 2]
        names = [name for task in tasks for name in task.company_names]
        assert len(names) == len(set(names)) == 17


def test_dependencies_from_env_builds_dynamo_backed_repositories(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://localhost:4566")
    monkeypatch.setenv("JOBS_TABLE_NAME", "env-jobs")
    deps = Dependencies.from_env()
    assert deps.settings.aws.jobs_table == "env-jobs"
    assert deps.settings.aws.endpoint_url == "http://localhost:4566"
    assert isinstance(deps.settings.aws, AwsSettings)
    assert len(deps.registry) >= 50
