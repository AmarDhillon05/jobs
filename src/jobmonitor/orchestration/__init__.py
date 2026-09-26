"""Orchestration: scheduling, sharding, workers and notification dispatch.

Selected architecture (see ARCHITECTURE.md for the alternatives considered)::

    EventBridge Scheduler (every 10 min)
            |
            v
    Coordinator Lambda ---- shards the registry (8 companies/shard)
            |
            v
        SQS scrape queue  --(after 3 attempts)-->  scrape DLQ
         /      |      \\
      Worker Lambdas (concurrent, one shard each)
            |
            +--> DynamoDB jobs table
            |
            v
        SNS notification topic
            |
            v
    SQS notification queue --(after 3 attempts)--> notification DLQ
            |
            v
      Notifier Lambda ---> email (SES) + push (SNS / Web Push)

    API Gateway HTTP API -> API Lambda -> DynamoDB -> PWA feed
"""

from jobmonitor.orchestration.handlers import (
    Dependencies,
    coordinator_handler,
    get_dependencies,
    notifier_handler,
    reset_dependencies,
    worker_handler,
)
from jobmonitor.orchestration.messages import ScheduledPollEvent, ScrapeTask
from jobmonitor.orchestration.pipeline import (
    CompanyOutcome,
    PollOutcome,
    PollRunner,
    StorageFailure,
    process_company,
)
from jobmonitor.orchestration.queues import (
    InMemoryQueue,
    InMemoryTopic,
    PublishError,
    QueuePublisher,
    SnsTopic,
    SqsQueue,
    TopicPublisher,
)

__all__ = [
    "CompanyOutcome",
    "Dependencies",
    "InMemoryQueue",
    "InMemoryTopic",
    "PollOutcome",
    "PollRunner",
    "PublishError",
    "QueuePublisher",
    "ScheduledPollEvent",
    "ScrapeTask",
    "SnsTopic",
    "SqsQueue",
    "StorageFailure",
    "TopicPublisher",
    "coordinator_handler",
    "get_dependencies",
    "notifier_handler",
    "process_company",
    "reset_dependencies",
    "worker_handler",
]
