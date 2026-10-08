"""Drafting free-text answers from the user's resume, with Claude.

The user asked for answers written **from their resume**, which they can then edit
or replace. So the resume PDF itself goes to Claude as a ``document`` block (no
lossy text extraction), together with the profile's facts, the job and the
question. The prompt forbids inventing experience: where the resume has nothing to
support an answer, the draft says so in ``[add: ...]`` brackets for the user to fill.

Request shape (per the Claude API guidance for Claude Opus 5.5):

* ``effort: "low"`` - a short answer; also keeps a draft well inside API Gateway's
  29-second limit. Thinking is always on for this model and is not configured.
* ``fallbacks: "default"`` (beta ``server-side-fallback-2026-07-01``) - if a
  safety classifier declines, Anthropic re-runs the request on its recommended
  fallback model inside the same call. A final ``stop_reason == "refusal"`` still
  surfaces as :class:`DraftRefused`.
* The resume block carries ``cache_control``: drafts for one kit reuse it.

The ``anthropic`` package is imported only here, and is shipped only to the Kit
Lambda (as a layer): every other function keeps the dependency-free bundle.
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger(__name__)

MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_TOKENS = 4000
#: Seconds. API Gateway gives the Kit Lambda 29 in total.
REQUEST_TIMEOUT = 25.0

SYSTEM_PROMPT = """\
You draft answers to internship application questions for a student, who will \
review and edit every word before using it.

Write in the first person, in a plain, specific, confident voice - the student's \
voice, not a cover-letter template. Use only facts found in the attached resume or \
the profile notes. Never invent experience, employers, numbers, skills or \
coursework. If the question asks for something the resume does not support, write \
the strongest honest answer you can and mark each gap as [add: what the student \
should fill in].

Match the question: one or two sentences for a short question, at most about 150 \
words for an open question unless it states a length. Tie the answer to this \
company and role using the job description when that helps. Reply with the answer \
text only: no preamble, no title, no quotation marks, no markdown."""


class DraftError(RuntimeError):
    """A draft could not be produced (network, API error, empty reply)."""


class DraftRefused(DraftError):
    """The model declined to write this answer."""


@dataclass(frozen=True, slots=True)
class JobContext:
    company: str
    title: str
    location: str | None = None
    description: str | None = None


class Drafter(Protocol):
    def draft(self, *, question: str, job: JobContext, current: str | None = None) -> str: ...


def build_request(
    *,
    question: str,
    job: JobContext,
    resume_pdf: bytes,
    profile_summary: str,
    current: str | None = None,
) -> dict[str, Any]:
    """The ``client.beta.messages.create`` arguments. Separate so tests can assert it."""
    parts = [
        f"Company: {job.company}",
        f"Role: {job.title}",
    ]
    if job.location:
        parts.append(f"Location: {job.location}")
    if job.description:
        parts.append(f"Job description:\n{job.description[:6000]}")
    if profile_summary:
        parts.append(f"Profile notes (facts from the student):\n{profile_summary}")
    if current and current.strip():
        parts.append(
            "The student's current answer, to improve while keeping their meaning and "
            f"anything they wrote themselves:\n{current.strip()}"
        )
    parts.append(f"Application question:\n{question.strip()}")
    return {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "betas": [FALLBACK_BETA],
        "fallbacks": "default",
        "output_config": {"effort": "low"},
        "system": SYSTEM_PROMPT,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": base64.standard_b64encode(resume_pdf).decode("ascii"),
                        },
                        "title": "Resume",
                        "cache_control": {"type": "ephemeral"},
                    },
                    {"type": "text", "text": "\n\n".join(parts)},
                ],
            }
        ],
    }


def answer_text(response: Any) -> str:
    """The answer from a Messages API response, or a clear error."""
    if getattr(response, "stop_reason", None) == "refusal":
        details = getattr(response, "stop_details", None)
        category = getattr(details, "category", None) if details is not None else None
        raise DraftRefused(f"Claude declined to draft this answer (category: {category})")
    text = "".join(
        getattr(block, "text", "")
        for block in getattr(response, "content", None) or ()
        if getattr(block, "type", None) == "text"
    ).strip()
    if not text:
        raise DraftError("Claude returned no text")
    return text


class ClaudeDrafter:
    """Drafts through the Anthropic API with the official SDK."""

    def __init__(
        self,
        *,
        api_key: str,
        resume_pdf: bytes,
        profile_summary: str = "",
        client: Any = None,
    ) -> None:
        if not resume_pdf:
            raise DraftError("no resume uploaded: run `make apply-setup` first")
        self.resume_pdf = resume_pdf
        self.profile_summary = profile_summary
        if client is None:
            import anthropic  # shipped to the Kit Lambda only, as a layer

            client = anthropic.Anthropic(api_key=api_key, timeout=REQUEST_TIMEOUT, max_retries=1)
        self.client = client

    def draft(self, *, question: str, job: JobContext, current: str | None = None) -> str:
        request = build_request(
            question=question,
            job=job,
            resume_pdf=self.resume_pdf,
            profile_summary=self.profile_summary,
            current=current,
        )
        try:
            response = self.client.beta.messages.create(**request)
        except Exception as exc:  # the SDK's typed errors all derive from Exception
            logger.warning("draft failed: %s: %s", type(exc).__name__, exc)
            raise DraftError(f"{type(exc).__name__}: {exc}") from exc
        usage = getattr(response, "usage", None)
        logger.info(
            "draft",
            extra={
                "structured": {
                    "company": job.company,
                    "input_tokens": getattr(usage, "input_tokens", None),
                    "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", None),
                    "output_tokens": getattr(usage, "output_tokens", None),
                }
            },
        )
        return answer_text(response)


class SampleDrafter:
    """A stand-in for local previews and tests: no network, no cost."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def draft(self, *, question: str, job: JobContext, current: str | None = None) -> str:
        self.calls.append({"question": question, "company": job.company, "current": current})
        base = (current or "").strip()
        return (f"{base}\n\n" if base else "") + (
            f"[Sample draft - set ANTHROPIC_API_KEY for real drafts] Answer to "
            f"'{question.strip()}' for {job.title} at {job.company}, written from your resume."
        )


__all__: Sequence[str] = (
    "FALLBACK_BETA",
    "MODEL",
    "SYSTEM_PROMPT",
    "ClaudeDrafter",
    "DraftError",
    "DraftRefused",
    "Drafter",
    "JobContext",
    "SampleDrafter",
    "answer_text",
    "build_request",
)
