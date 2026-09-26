"""HTTP client with a bounded retry policy (PRD §17).

Design note: the client talks to an injected *transport* callable rather than to
a library directly. That is what lets Level-2 tests drive an exact status
sequence (``500, 500, 200``; ``429 + Retry-After``) with no network, no clock and
no mocking library, while production uses :class:`Urllib3Transport` - urllib3
being already present in the AWS Lambda Python runtime, so the deployment asset
needs no dependency bundling at all.
"""

from __future__ import annotations

import json
import random
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

from jobmonitor.config import HttpSettings
from jobmonitor.errors import (
    AccessBlocked,
    HttpStatusError,
    NetworkError,
    ParseError,
    RateLimited,
    RetryBudgetExhausted,
    ServerError,
)

#: Statuses we retry. Everything else 4xx is a permanent client error.
RETRYABLE_STATUSES: frozenset[int] = frozenset({408, 425, 429, 500, 502, 503, 504, 507, 509})
MAX_RETRY_AFTER_SECONDS = 300.0
"""A source asking us to wait longer than this is treated as "come back next
poll" rather than holding a Lambda invocation open."""


@dataclass(frozen=True, slots=True)
class HttpRequest:
    url: str
    method: str = "GET"
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes | None = None
    timeout: float = 15.0


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    body: bytes
    headers: Mapping[str, str] = field(default_factory=dict)
    url: str = ""

    def header(self, name: str) -> str | None:
        """Case-insensitive header lookup."""
        lowered = name.lower()
        for key, value in self.headers.items():
            if key.lower() == lowered:
                return value
        return None

    def json(self) -> Any:
        if not self.body:
            raise ParseError(f"{self.url}: empty response body where JSON was expected")
        try:
            return json.loads(self.body)
        except (ValueError, UnicodeDecodeError) as exc:
            preview = self.body[:200].decode("utf-8", "replace")
            raise ParseError(f"{self.url}: response was not valid JSON ({preview!r})") from exc

    def text(self, encoding: str = "utf-8") -> str:
        return self.body.decode(encoding, "replace")


#: A transport turns a request into a response, or raises
#: :class:`~jobmonitor.errors.NetworkError` for a transport-level failure.
Transport = Callable[[HttpRequest], HttpResponse]


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """Interpret a ``Retry-After`` header: delta-seconds *or* an HTTP date."""
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    reference = now or datetime.now(UTC)
    return max(0.0, (when - reference).total_seconds())


@dataclass(slots=True)
class AttemptLog:
    """What the client actually did, so tests and health records can assert it."""

    attempts: int = 0
    statuses: list[int] = field(default_factory=list)
    sleeps: list[float] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class HttpClient:
    """Bounded-retry HTTP client with exponential backoff and full jitter."""

    def __init__(
        self,
        settings: HttpSettings | None = None,
        *,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self.settings = settings or HttpSettings()
        self._transport = transport or Urllib3Transport()
        self._sleep = sleep
        self._rng = rng or random.Random()
        self.log = AttemptLog()

    # ------------------------------------------------------------------ backoff
    def backoff_delay(self, attempt: int, *, retry_after: float | None = None) -> float:
        """Delay before ``attempt`` (1-based index of the attempt about to fail).

        ``Retry-After`` always wins when the server supplied one - that is the
        source telling us its own rate limit, and guessing lower would be
        hammering it (PRD §17).
        """
        if retry_after is not None:
            return min(retry_after, MAX_RETRY_AFTER_SECONDS)
        base = self.settings.backoff_base_seconds
        if base <= 0:
            return 0.0
        uncapped = base * (2 ** max(0, attempt - 1))
        capped = min(uncapped, self.settings.backoff_max_seconds)
        # Full jitter: uniform in [0, capped]. Avoids synchronized retry storms
        # across the worker fleet all hitting one ATS at the same instant.
        return self._rng.uniform(0.0, capped)

    # ------------------------------------------------------------------ request
    def request(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
        json_body: Any = None,
        timeout: float | None = None,
    ) -> HttpResponse:
        if json_body is not None:
            if body is not None:
                raise ValueError("pass either body or json_body, not both")
            body = json.dumps(json_body).encode("utf-8")

        merged: dict[str, str] = {
            "User-Agent": self.settings.user_agent,
            "Accept": "application/json, text/plain, */*",
            "Accept-Encoding": "gzip, deflate",
        }
        if json_body is not None:
            merged["Content-Type"] = "application/json"
        if headers:
            merged.update(headers)

        request = HttpRequest(
            url=url,
            method=method.upper(),
            headers=merged,
            body=body,
            timeout=timeout if timeout is not None else self.settings.timeout_seconds,
        )

        max_attempts = max(1, self.settings.max_attempts)
        last_error: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            self.log.attempts = attempt
            try:
                response = self._transport(request)
            except NetworkError as exc:
                last_error = exc
                self.log.errors.append(f"{type(exc).__name__}: {exc}")
                if attempt == max_attempts:
                    break
                self._wait(self.backoff_delay(attempt))
                continue

            self.log.statuses.append(response.status)

            if 200 <= response.status < 300:
                return response

            error = self._classify(response)
            if isinstance(error, RateLimited):
                last_error = error
                self.log.errors.append(f"RateLimited: {error}")
                if attempt == max_attempts:
                    break
                self._wait(self.backoff_delay(attempt, retry_after=error.retry_after))
                continue
            if isinstance(error, ServerError):
                last_error = error
                self.log.errors.append(f"ServerError: {error}")
                if attempt == max_attempts:
                    break
                self._wait(self.backoff_delay(attempt))
                continue
            # Permanent: do not spend the remaining budget on it.
            raise error

        assert last_error is not None
        raise RetryBudgetExhausted(
            f"{request.method} {url}: giving up after {max_attempts} attempt(s); "
            f"last error: {last_error}",
            attempts=max_attempts,
            last_error=last_error,
        )

    def get_json(self, url: str, **kwargs: Any) -> Any:
        return self.request(url, **kwargs).json()

    def post_json(self, url: str, payload: Any, **kwargs: Any) -> Any:
        return self.request(url, method="POST", json_body=payload, **kwargs).json()

    # ------------------------------------------------------------------ helpers
    def _wait(self, delay: float) -> None:
        if delay > 0:
            self.log.sleeps.append(delay)
            self._sleep(delay)
        else:
            self.log.sleeps.append(0.0)

    def _classify(self, response: HttpResponse) -> Exception:
        status = response.status
        where = response.url or "<unknown url>"
        if status == 429:
            return RateLimited(
                f"{where}: rate limited (429)",
                retry_after=parse_retry_after(response.header("Retry-After")),
            )
        if status in {401, 403}:
            return AccessBlocked(
                f"{where}: source refused automated access (HTTP {status}); "
                "marking blocked rather than attempting to bypass"
            )
        if status in RETRYABLE_STATUSES or 500 <= status < 600:
            return ServerError(f"{where}: server error (HTTP {status})", status=status)
        return HttpStatusError(f"{where}: unexpected status HTTP {status}", status=status)


class Urllib3Transport:
    """Production transport. urllib3 ships with the Lambda Python runtime."""

    def __init__(self, pool: Any = None) -> None:
        self._pool = pool

    def _get_pool(self) -> Any:
        if self._pool is None:
            import urllib3

            self._pool = urllib3.PoolManager(
                retries=False,  # retries are this module's job, not urllib3's
                maxsize=10,
                timeout=None,
            )
        return self._pool

    def __call__(self, request: HttpRequest) -> HttpResponse:
        import urllib3
        from urllib3.exceptions import HTTPError as Urllib3HTTPError

        pool = self._get_pool()
        try:
            raw = pool.request(
                request.method,
                request.url,
                headers=dict(request.headers),
                body=request.body,
                timeout=urllib3.Timeout(total=request.timeout),
                preload_content=True,
                redirect=True,
            )
        except Urllib3HTTPError as exc:
            # Timeouts, connection resets, DNS failures: all retryable.
            raise NetworkError(
                f"{request.method} {request.url}: {type(exc).__name__}: {exc}"
            ) from exc
        except OSError as exc:
            raise NetworkError(f"{request.method} {request.url}: {exc}") from exc

        return HttpResponse(
            status=raw.status,
            body=raw.data or b"",
            headers=dict(raw.headers),
            url=request.url,
        )


__all__: Sequence[str] = (
    "MAX_RETRY_AFTER_SECONDS",
    "RETRYABLE_STATUSES",
    "AttemptLog",
    "HttpClient",
    "HttpRequest",
    "HttpResponse",
    "Transport",
    "Urllib3Transport",
    "parse_retry_after",
)
