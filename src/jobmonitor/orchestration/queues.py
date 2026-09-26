"""Queue and topic publishers.

Same pattern as the storage and notification layers: an interface with a real
implementation and an in-memory one, so the whole fan-out can be asserted without
AWS, and the same code runs against SQS/SNS under Moto and LocalStack.

Batching matters here: ``send_message_batch`` takes 10 messages per call, so a
150-company registry at 8 companies per shard is 19 shards - two API calls, not
nineteen. The batch API reports per-message failures, which are surfaced rather
than swallowed: a shard that never reached the queue is a shard that never runs.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from jobmonitor.errors import JobMonitorError

SQS_BATCH_LIMIT = 10


class PublishError(JobMonitorError):
    """A message could not be published."""


@dataclass(frozen=True, slots=True)
class PublishResult:
    successful: tuple[str, ...] = ()
    failed: tuple[tuple[str, str], ...] = ()

    @property
    def ok(self) -> bool:
        return not self.failed

    @property
    def count(self) -> int:
        return len(self.successful)


class QueuePublisher(ABC):
    @abstractmethod
    def publish(self, bodies: Sequence[str]) -> PublishResult:
        """Enqueue each body. Ids in the result are the caller's own indices."""


@dataclass
class InMemoryQueue(QueuePublisher):
    """Records bodies. Doubles as the in-process transport for `make local`."""

    name: str = "memory-queue"
    bodies: list[str] = field(default_factory=list)
    #: 0-based indices to fail, to exercise partial-batch handling.
    fail_indices: frozenset[int] = frozenset()

    def publish(self, bodies: Sequence[str]) -> PublishResult:
        successful: list[str] = []
        failed: list[tuple[str, str]] = []
        for index, body in enumerate(bodies):
            if index in self.fail_indices:
                failed.append((str(index), "simulated publish failure"))
                continue
            self.bodies.append(body)
            successful.append(str(index))
        return PublishResult(tuple(successful), tuple(failed))

    def drain(self) -> list[str]:
        bodies, self.bodies = self.bodies, []
        return bodies

    def clear(self) -> None:
        self.bodies.clear()


class SqsQueue(QueuePublisher):
    def __init__(
        self,
        queue_url: str | None,
        *,
        client: Any = None,
        region: str = "us-east-1",
        endpoint_url: str | None = None,
    ) -> None:
        self.queue_url = queue_url
        self._client = client
        self._region = region
        self._endpoint_url = endpoint_url

    def _get_client(self) -> Any:
        if self._client is None:
            import boto3

            kwargs: dict[str, Any] = {"region_name": self._region}
            if self._endpoint_url:
                kwargs["endpoint_url"] = self._endpoint_url
            self._client = boto3.client("sqs", **kwargs)
        return self._client

    def publish(self, bodies: Sequence[str]) -> PublishResult:
        from botocore.exceptions import BotoCoreError, ClientError

        if not self.queue_url:
            raise PublishError("SqsQueue needs SCRAPE_QUEUE_URL to be configured")
        if not bodies:
            return PublishResult()

        client = self._get_client()
        successful: list[str] = []
        failed: list[tuple[str, str]] = []

        for start in range(0, len(bodies), SQS_BATCH_LIMIT):
            chunk = bodies[start : start + SQS_BATCH_LIMIT]
            entries = [
                {"Id": str(start + offset), "MessageBody": body}
                for offset, body in enumerate(chunk)
            ]
            try:
                response = client.send_message_batch(QueueUrl=self.queue_url, Entries=entries)
            except (ClientError, BotoCoreError) as exc:
                raise PublishError(f"SQS send_message_batch failed: {exc}") from exc
            successful.extend(entry["Id"] for entry in response.get("Successful", []))
            failed.extend(
                (entry.get("Id", "?"), entry.get("Message", "unknown"))
                for entry in response.get("Failed", [])
            )
        return PublishResult(tuple(successful), tuple(failed))


class TopicPublisher(ABC):
    @abstractmethod
    def publish(self, body: str, *, subject: str | None = None) -> str: ...


@dataclass
class InMemoryTopic(TopicPublisher):
    name: str = "memory-topic"
    messages: list[tuple[str, str | None]] = field(default_factory=list)
    fail_next: bool = False

    def publish(self, body: str, *, subject: str | None = None) -> str:
        if self.fail_next:
            self.fail_next = False
            raise PublishError("simulated topic failure")
        self.messages.append((body, subject))
        return f"memory-topic-{len(self.messages)}"

    def bodies(self) -> list[str]:
        return [body for body, _subject in self.messages]

    def clear(self) -> None:
        self.messages.clear()


class SnsTopic(TopicPublisher):
    def __init__(
        self,
        topic_arn: str | None,
        *,
        client: Any = None,
        region: str = "us-east-1",
        endpoint_url: str | None = None,
    ) -> None:
        self.topic_arn = topic_arn
        self._client = client
        self._region = region
        self._endpoint_url = endpoint_url

    def _get_client(self) -> Any:
        if self._client is None:
            import boto3

            kwargs: dict[str, Any] = {"region_name": self._region}
            if self._endpoint_url:
                kwargs["endpoint_url"] = self._endpoint_url
            self._client = boto3.client("sns", **kwargs)
        return self._client

    def publish(self, body: str, *, subject: str | None = None) -> str:
        from botocore.exceptions import BotoCoreError, ClientError

        if not self.topic_arn:
            raise PublishError("SnsTopic needs NOTIFICATION_TOPIC_ARN to be configured")
        request: dict[str, Any] = {"TopicArn": self.topic_arn, "Message": body}
        if subject:
            request["Subject"] = subject[:99]
        try:
            response = self._get_client().publish(**request)
        except (ClientError, BotoCoreError) as exc:
            raise PublishError(f"SNS publish failed: {exc}") from exc
        return str(response.get("MessageId", ""))


__all__: Sequence[str] = (
    "SQS_BATCH_LIMIT",
    "InMemoryQueue",
    "InMemoryTopic",
    "PublishError",
    "PublishResult",
    "QueuePublisher",
    "SnsTopic",
    "SqsQueue",
    "TopicPublisher",
)
