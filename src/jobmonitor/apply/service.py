"""The Apply kit's logic, independent of HTTP: build a kit, draft, save, resume.

Answer precedence for each question on a kit, highest first:

1. what the user wrote or saved **for this job** (``user``);
2. a draft already made for this job (``draft``), so reopening costs nothing;
3. the user's **library** answer for the same question (``library``);
4. the **profile** field that answers it (``profile``);
5. nothing: a free-text question is drafted when the page asks for it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from jobmonitor.apply import forms
from jobmonitor.apply.drafter import Drafter, DraftError, JobContext
from jobmonitor.apply.profile import Profile
from jobmonitor.apply.questions import Question, fetch_greenhouse
from jobmonitor.apply.store import DRAFT, LIBRARY, PROFILE, USER, Answer, KitStore
from jobmonitor.errors import JobMonitorError
from jobmonitor.http import HttpClient
from jobmonitor.models.company import CompanyRegistry
from jobmonitor.models.record import JobRecord
from jobmonitor.storage.base import JobRepository

logger = logging.getLogger(__name__)

DRAFT_DAILY_LIMIT = 60


class KitNotFound(LookupError):
    """No such job, or not one a kit can be made for (events have no application)."""


class DraftLimitReached(RuntimeError):
    """Today's draft budget is used up."""


@dataclass(frozen=True, slots=True)
class FilledQuestion:
    question: Question
    answer: Answer | None

    @property
    def needs_draft(self) -> bool:
        return self.answer is None and self.question.draftable


@dataclass(frozen=True, slots=True)
class FilledStep:
    step: forms.Step
    questions: tuple[FilledQuestion, ...]


@dataclass(slots=True)
class Kit:
    job: JobRecord
    walkthrough: forms.Walkthrough
    steps: list[FilledStep] = field(default_factory=list)
    #: Questions the user pasted on this job before, so they come back on reload.
    extra: list[Answer] = field(default_factory=list)
    resume_available: bool = False


class KitService:
    def __init__(
        self,
        *,
        repository: JobRepository,
        store: KitStore,
        profile: Callable[[], Profile],
        drafter: Callable[[], Drafter],
        registry: CompanyRegistry | None = None,
        http: HttpClient | None = None,
        resume_available: Callable[[], bool] = lambda: False,
        draft_daily_limit: int = DRAFT_DAILY_LIMIT,
        today: Callable[[], date] = lambda: datetime.now(UTC).date(),
    ) -> None:
        self.repository = repository
        self.store = store
        self._profile = profile
        self._drafter = drafter
        self.registry = registry
        self.http = http
        self.resume_available = resume_available
        self.draft_daily_limit = draft_daily_limit
        self.today = today

    # --------------------------------------------------------------- helpers
    def job(self, job_id: str) -> JobRecord:
        record = self.repository.get(job_id)
        if record is None:
            raise KitNotFound(job_id)
        from jobmonitor.models.job import is_event_kind

        if is_event_kind(record.employment_type):
            raise KitNotFound(f"{job_id} is an event, not an application")
        return record

    def _form_questions(self, record: JobRecord, ats: str) -> list[Question]:
        """The job's real form questions (Greenhouse only), fetched once and cached."""
        if ats != forms.GREENHOUSE or not record.external_id:
            return []
        cached = self.store.get_questions(record.job_id)
        if cached is not None:
            return [Question.from_dict(q) for q in cached.get("questions", [])]
        board = self._greenhouse_board(record)
        if not board or self.http is None:
            return []
        try:
            fetched = fetch_greenhouse(self.http, board, str(record.external_id))
        except JobMonitorError as exc:
            logger.warning("kit: could not fetch questions for %s: %s", record.job_id, exc)
            return []
        self.store.put_questions(
            record.job_id,
            {
                "questions": [q.to_dict() for q in fetched.questions],
                "description": fetched.description or "",
            },
        )
        return fetched.questions

    def _greenhouse_board(self, record: JobRecord) -> str | None:
        if self.registry is None:
            return None
        company = self.registry.get(record.company)
        if company is None:
            return None
        config = company.provider_config
        if config.get("board_token"):
            return str(config["board_token"])
        # Runtime-resolved boards: the posting URL names the board it came from.
        url = record.url
        marker = "greenhouse.io/"
        if marker in url:
            tail = url.split(marker, 1)[1].split("/")
            if tail and tail[0] not in {"embed", "v1"}:
                return tail[0]
        return None

    def _profile_answer(self, profile: Profile, question: Question) -> Answer | None:
        key = question.profile_key
        if not key:
            return None
        if key == "__full_name__":
            value = profile.full_name or None
        elif key == "__today__":
            value = f"{self.today():%m/%d/%Y}"
        else:
            value = profile.value(key)
        if not value:
            return None
        return Answer(question=question.label, text=value, source=PROFILE, updated_at="")

    def _answer_for(self, job_id: str, question: Question, profile: Profile) -> Answer | None:
        stored = self.store.get_answer(job_id, question.label)
        if stored is not None:
            return stored
        library = self.store.library_get(question.label) or profile.library.get(question.label)
        if library:
            return Answer(question=question.label, text=library, source=LIBRARY, updated_at="")
        return self._profile_answer(profile, question)

    def context(self, record: JobRecord) -> JobContext:
        cached = self.store.get_questions(record.job_id) or {}
        return JobContext(
            company=record.company,
            title=record.title,
            location=record.location,
            description=record.description or cached.get("description") or None,
        )

    # ------------------------------------------------------------------- API
    def build(self, job_id: str) -> Kit:
        record = self.job(job_id)
        profile = self._profile()
        ats = forms.detect_ats(record.url, record.source)
        walkthrough = forms.walkthrough_for(
            ats, self._form_questions(record, ats), today=self.today()
        )
        kit = Kit(job=record, walkthrough=walkthrough, resume_available=self.resume_available())
        shown: set[str] = set()
        for step in walkthrough.steps:
            filled = tuple(
                FilledQuestion(q, self._answer_for(record.job_id, q, profile))
                for q in step.questions
            )
            shown.update(q.label for q in step.questions)
            kit.steps.append(FilledStep(step, filled))
        kit.extra = [a for a in self.store.answers_for(record.job_id) if a.question not in shown]
        return kit

    def draft(
        self, job_id: str, question: str, *, current: str | None = None, redraft: bool = False
    ) -> Answer:
        record = self.job(job_id)
        question = question.strip()
        if not question:
            raise ValueError("question is empty")
        if not redraft:
            stored = self.store.get_answer(job_id, question)
            if stored is not None:
                return stored
            library = self.store.library_get(question) or self._profile().library.get(question)
            if library:
                return Answer(question=question, text=library, source=LIBRARY, updated_at="")
        if not self.store.take_draft(self.today().isoformat(), self.draft_daily_limit):
            raise DraftLimitReached(
                f"{self.draft_daily_limit} drafts used today; write this one yourself "
                "or try again tomorrow"
            )
        text = self._drafter().draft(question=question, job=self.context(record), current=current)
        return self.store.put_answer(job_id, question, text, DRAFT)

    def save(self, job_id: str, question: str, text: str, *, to_library: bool = False) -> Answer:
        self.job(job_id)
        question = question.strip()
        if not question:
            raise ValueError("question is empty")
        answer = self.store.put_answer(job_id, question, text, USER)
        if to_library and text.strip():
            self.store.library_put(question, text)
        return answer


__all__: Sequence[str] = (
    "DRAFT_DAILY_LIMIT",
    "DraftError",
    "DraftLimitReached",
    "FilledQuestion",
    "FilledStep",
    "Kit",
    "KitNotFound",
    "KitService",
)
