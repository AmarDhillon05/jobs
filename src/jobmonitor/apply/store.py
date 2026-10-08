"""What the Apply kit remembers: drafts, your own answers, and the draft budget.

* **Per job**: the fetched form questions (so reopening a kit makes no request) and
  every answer shown on it, drafted or written by the user, so reopening costs
  nothing and an edit is never lost.
* **Library**: answers the user saved for reuse. The next form that asks the same
  question (compared by :func:`normalize_question`) shows the saved answer instead
  of spending a new draft.
* **Quota**: drafts per UTC day, capped (``DRAFT_DAILY_LIMIT``), so a leaked link
  cannot run up the Claude bill.

One DynamoDB table, ``pk``/``sk`` strings; an in-memory twin runs tests and local
previews, exactly as the job repositories do.
"""

from __future__ import annotations

import hashlib
import threading
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from jobmonitor.apply.profile import normalize_question

DRAFT, USER, LIBRARY, PROFILE = "draft", "user", "library", "profile"
#: Quota rows expire on their own a couple of days later.
QUOTA_TTL = timedelta(days=3)


def question_hash(question: str) -> str:
    return hashlib.sha256(normalize_question(question).encode()).hexdigest()[:24]


@dataclass(frozen=True, slots=True)
class Answer:
    question: str
    text: str
    #: ``draft`` (Claude), ``user`` (typed or edited), ``library`` (a saved answer)
    source: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "text": self.text,
            "source": self.source,
            "updated_at": self.updated_at,
        }


def _now() -> str:
    return datetime.now(UTC).isoformat()


class KitStore(ABC):
    @abstractmethod
    def get_questions(self, job_id: str) -> dict[str, Any] | None: ...

    @abstractmethod
    def put_questions(self, job_id: str, payload: Mapping[str, Any]) -> None: ...

    @abstractmethod
    def get_answer(self, job_id: str, question: str) -> Answer | None: ...

    @abstractmethod
    def put_answer(self, job_id: str, question: str, text: str, source: str) -> Answer: ...

    @abstractmethod
    def answers_for(self, job_id: str) -> list[Answer]: ...

    @abstractmethod
    def library_get(self, question: str) -> str | None: ...

    @abstractmethod
    def library_put(self, question: str, text: str) -> None: ...

    @abstractmethod
    def take_draft(self, day: str, limit: int) -> bool:
        """Count one draft against ``day``; ``False`` once ``limit`` is reached."""


class InMemoryKitStore(KitStore):
    def __init__(self) -> None:
        self._questions: dict[str, dict[str, Any]] = {}
        self._answers: dict[tuple[str, str], Answer] = {}
        self._library: dict[str, str] = {}
        self._quota: dict[str, int] = {}
        self._lock = threading.RLock()

    def get_questions(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._questions.get(job_id)

    def put_questions(self, job_id: str, payload: Mapping[str, Any]) -> None:
        with self._lock:
            self._questions[job_id] = dict(payload)

    def get_answer(self, job_id: str, question: str) -> Answer | None:
        with self._lock:
            return self._answers.get((job_id, question_hash(question)))

    def put_answer(self, job_id: str, question: str, text: str, source: str) -> Answer:
        answer = Answer(question=question, text=text, source=source, updated_at=_now())
        with self._lock:
            self._answers[(job_id, question_hash(question))] = answer
        return answer

    def answers_for(self, job_id: str) -> list[Answer]:
        with self._lock:
            return [a for (jid, _h), a in self._answers.items() if jid == job_id]

    def library_get(self, question: str) -> str | None:
        with self._lock:
            return self._library.get(question_hash(question))

    def library_put(self, question: str, text: str) -> None:
        with self._lock:
            self._library[question_hash(question)] = text

    def take_draft(self, day: str, limit: int) -> bool:
        with self._lock:
            used = self._quota.get(day, 0)
            if used >= limit:
                return False
            self._quota[day] = used + 1
            return True


class DynamoKitStore(KitStore):
    def __init__(self, table: Any) -> None:
        self.table = table

    @classmethod
    def from_settings(cls, settings: Any, *, resource: Any = None) -> DynamoKitStore:
        from jobmonitor.storage.dynamo import build_resource

        return cls(build_resource(settings, resource=resource).Table(settings.apply_table))

    def get_questions(self, job_id: str) -> dict[str, Any] | None:
        item = self.table.get_item(Key={"pk": f"JOB#{job_id}", "sk": "FORM"}).get("Item")
        return dict(item["form"]) if item and "form" in item else None

    def put_questions(self, job_id: str, payload: Mapping[str, Any]) -> None:
        self.table.put_item(
            Item={"pk": f"JOB#{job_id}", "sk": "FORM", "form": dict(payload), "updated_at": _now()}
        )

    @staticmethod
    def _answer(item: Mapping[str, Any]) -> Answer:
        return Answer(
            question=str(item["question"]),
            text=str(item["text"]),
            source=str(item.get("source") or USER),
            updated_at=str(item.get("updated_at") or ""),
        )

    def get_answer(self, job_id: str, question: str) -> Answer | None:
        item = self.table.get_item(
            Key={"pk": f"JOB#{job_id}", "sk": f"Q#{question_hash(question)}"}
        ).get("Item")
        return self._answer(item) if item else None

    def put_answer(self, job_id: str, question: str, text: str, source: str) -> Answer:
        answer = Answer(question=question, text=text, source=source, updated_at=_now())
        self.table.put_item(
            Item={
                "pk": f"JOB#{job_id}",
                "sk": f"Q#{question_hash(question)}",
                **answer.to_dict(),
            }
        )
        return answer

    def answers_for(self, job_id: str) -> list[Answer]:
        from boto3.dynamodb.conditions import Key

        items: list[Mapping[str, Any]] = []
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": Key("pk").eq(f"JOB#{job_id}") & Key("sk").begins_with("Q#")
        }
        while True:
            page = self.table.query(**kwargs)
            items.extend(page.get("Items", []))
            if "LastEvaluatedKey" not in page:
                break
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        return [self._answer(item) for item in items]

    def library_get(self, question: str) -> str | None:
        item = self.table.get_item(Key={"pk": "LIBRARY", "sk": f"Q#{question_hash(question)}"}).get(
            "Item"
        )
        return str(item["text"]) if item else None

    def library_put(self, question: str, text: str) -> None:
        self.table.put_item(
            Item={
                "pk": "LIBRARY",
                "sk": f"Q#{question_hash(question)}",
                "question": question,
                "text": text,
                "updated_at": _now(),
            }
        )

    def take_draft(self, day: str, limit: int) -> bool:
        from botocore.exceptions import ClientError

        expires = int((datetime.now(UTC) + QUOTA_TTL).timestamp())
        try:
            # Atomic: the condition and the increment are one write, so two drafts
            # racing for the last slot cannot both get it.
            self.table.update_item(
                Key={"pk": f"QUOTA#{day}", "sk": "DRAFTS"},
                UpdateExpression="ADD used :one SET expires_at = :exp",
                ConditionExpression="attribute_not_exists(used) OR used < :limit",
                ExpressionAttributeValues={":one": 1, ":limit": limit, ":exp": expires},
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return False
            raise
        return True


def apply_table_spec(table_name: str) -> dict[str, Any]:
    """``create_table`` kwargs (LocalStack/Moto); CDK defines the same table."""
    return {
        "TableName": table_name,
        "BillingMode": "PAY_PER_REQUEST",
        "KeySchema": [
            {"AttributeName": "pk", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        "AttributeDefinitions": [
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
        ],
    }


__all__: Sequence[str] = (
    "DRAFT",
    "LIBRARY",
    "PROFILE",
    "USER",
    "Answer",
    "DynamoKitStore",
    "InMemoryKitStore",
    "KitStore",
    "apply_table_spec",
    "question_hash",
)
