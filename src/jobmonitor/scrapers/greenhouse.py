"""Greenhouse job-board adapter - 52 companies in the shipped registry.

Endpoint (public, documented, no authentication):

    GET https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true

Response shape encoded by ``tests/fixtures/greenhouse/*.json``::

    {"jobs": [{"id": 4020160008,
               "title": "Software Engineer Intern",
               "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/4020160008",
               "location": {"name": "San Francisco, CA"},
               "offices": [{"name": "San Francisco"}],
               "departments": [{"name": "Engineering"}],
               "updated_at": "2026-09-01T12:00:00-04:00",
               "first_published": "2026-08-28T09:00:00-04:00",
               "requisition_id": "R-1234",
               "content": "&lt;p&gt;HTML-escaped description&lt;/p&gt;",
               "metadata": [...]}],
     "meta": {"total": 1}}

Two Greenhouse quirks the adapter handles deliberately:

* ``content`` is *HTML-escaped HTML* - it must be unescaped before tags are
  stripped, or the user reads ``&lt;p&gt;`` in their email.
* ``first_published`` is the posting date; ``updated_at`` changes when a
  recruiter edits anything. Using ``updated_at`` as the posting date would make
  old jobs look new, so it is only a fallback.

The board API returns the complete board in one response, so ``fetch_pages``
yields once; :meth:`fetch_pages` still asserts the payload's own ``meta.total``
against what it parsed, so a future paginated response cannot be silently
truncated.
"""

from __future__ import annotations

import html
from collections.abc import Iterator, Sequence
from typing import Any, ClassVar

from jobmonitor.errors import HttpStatusError, InvalidJobError, ParseError, ProviderConfigError
from jobmonitor.models.job import Job
from jobmonitor.scrapers._parse import as_mapping, as_sequence, first_of, join_locations, text_of
from jobmonitor.scrapers.base import JobSource, register

API_ROOT = "https://boards-api.greenhouse.io/v1/boards"


@register
class GreenhouseSource(JobSource):
    provider: ClassVar[str] = "greenhouse"
    #: Either `board_token` (known) or `board_token_candidates` (resolved live).
    required_config: ClassVar[tuple[str, ...]] = ()

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._resolved_token: str | None = None
        self._resolved_payload: dict[str, Any] | None = None

    def _validate_config(self) -> None:
        if not self.config.get("board_token") and not self.config.get("board_token_candidates"):
            raise ProviderConfigError(
                f"{self.company.company}: greenhouse adapter requires either "
                "'board_token' or 'board_token_candidates' in provider_config"
            )

    @property
    def board_token_candidates(self) -> tuple[str, ...]:
        """Board tokens to try, in order.

        Several companies run a Greenhouse board behind their own domain
        (stripe.com/jobs, careers.datadoghq.com, ...). Their posting URLs carry
        Greenhouse's `gh_jid` job id, which proves the provider, but never the
        *board token* - so the token cannot be recovered from observed data the
        way every other company's config was. Rather than hard-code a guess and
        call it verified, such a company is configured with candidates and the
        real token is resolved from the live API on first fetch.
        """
        single = self.config.get("board_token")
        if single:
            return (str(single),)
        return tuple(str(c) for c in as_sequence(self.config.get("board_token_candidates")) if c)

    @staticmethod
    def jobs_url_for(board_token: str) -> str:
        return f"{API_ROOT}/{board_token}/jobs?content=true"

    @property
    def board_token(self) -> str:
        """The resolved token. Triggers resolution if it has not happened yet."""
        return self._resolved_token or self.board_token_candidates[0]

    @property
    def jobs_url(self) -> str:
        return self.jobs_url_for(self.board_token)

    def resolve_board_token(self) -> str:
        """Pick the candidate whose board actually answers.

        A single configured token is trusted and returned without a probe, so the
        139 companies whose token came from observed URLs keep making exactly one
        request. Only multi-candidate entries probe, once per instance, and the
        winning response is reused rather than fetched twice.
        """
        if self._resolved_token is not None:
            return self._resolved_token

        candidates = self.board_token_candidates
        if not candidates:
            raise ProviderConfigError(f"{self.describe()}: no board token configured")
        if len(candidates) == 1:
            self._resolved_token = candidates[0]
            return self._resolved_token

        attempts: list[str] = []
        for candidate in candidates:
            try:
                payload = self.client.get_json(self.jobs_url_for(candidate))
            except HttpStatusError as exc:
                # 404 means "no such board": try the next candidate. Anything
                # else (403, 5xx after retries) is a real failure and must not be
                # disguised as a wrong guess.
                if exc.status == 404:
                    attempts.append(f"{candidate}=404")
                    continue
                raise
            except ParseError as exc:
                attempts.append(f"{candidate}=unparseable ({exc})")
                continue
            if isinstance(payload, dict) and isinstance(payload.get("jobs"), list):
                self._resolved_token = candidate
                self._resolved_payload = payload
                return candidate
            attempts.append(f"{candidate}=no jobs key")

        raise ParseError(
            f"{self.describe()}: none of the candidate Greenhouse boards responded "
            f"({', '.join(attempts)}). Re-run `make validate-companies` after "
            "checking the company's careers page for its real board token."
        )

    def fetch_pages(self) -> Iterator[Sequence[Any]]:
        self.resolve_board_token()
        if self._resolved_payload is not None:
            payload: Any = self._resolved_payload
            self._resolved_payload = None
        else:
            payload = self.client.get_json(self.jobs_url)
        if not isinstance(payload, dict):
            raise ParseError(
                f"{self.describe()}: expected a JSON object from {self.jobs_url}, "
                f"got {type(payload).__name__}"
            )
        jobs = payload.get("jobs")
        if jobs is None:
            raise ParseError(f"{self.describe()}: response has no 'jobs' key")
        if not isinstance(jobs, list):
            raise ParseError(f"{self.describe()}: 'jobs' was {type(jobs).__name__}, expected list")

        declared = first_of(payload, "meta.total")
        if isinstance(declared, int) and declared > len(jobs):
            # Greenhouse does not paginate this endpoint; if that ever changes we
            # must not quietly report a subset as "all current postings".
            raise ParseError(
                f"{self.describe()}: board declares {declared} jobs but returned "
                f"{len(jobs)} - refusing to treat a partial board as complete"
            )
        yield jobs

    def normalize(self, raw_job: Any) -> Job:
        record = as_mapping(raw_job)
        if not record:
            raise InvalidJobError(f"{self.describe()}: job record was not an object")

        url = text_of(record, "absolute_url")
        if not url:
            raise InvalidJobError(f"{self.describe()}: job {record.get('id')} has no absolute_url")

        locations = [text_of(record, "location.name")]
        locations.extend(
            text_of(as_mapping(office), "name") for office in as_sequence(record.get("offices"))
        )

        description = record.get("content")
        if isinstance(description, str):
            # Escaped HTML: unescape first, then let Job strip the tags.
            description = html.unescape(description)

        external_id = text_of(record, "id", "internal_job_id", "requisition_id")

        return Job(
            company=self.company.company,
            title=text_of(record, "title") or "",
            url=url,
            source=self.provider,
            location=join_locations(locations),
            external_id=external_id,
            # first_published is the posting date; updated_at moves on every edit.
            date_posted=first_of(record, "first_published", "updated_at"),
            description=description,
            employment_type=self._employment_type(record),
        )

    @staticmethod
    def _employment_type(record: Any) -> str | None:
        """Greenhouse exposes employment type only through board metadata."""
        for entry in as_sequence(as_mapping(record).get("metadata")):
            item = as_mapping(entry)
            name = (text_of(item, "name") or "").casefold()
            if name in {"employment type", "job type", "type"}:
                value = item.get("value")
                if isinstance(value, list):
                    return join_locations(str(v) for v in value)
                return text_of(item, "value")
        return None


__all__: Sequence[str] = ("API_ROOT", "GreenhouseSource")
