"""Error taxonomy.

The split that matters everywhere else in the system is *transient vs permanent*:

* :class:`TransientError` - retrying might work (timeout, reset, 5xx, 429).
* :class:`PermanentError` - retrying cannot work (403, 404, unparseable payload).

The HTTP client retries the first kind and gives up immediately on the second,
and the orchestrator records either as a scraper failure without aborting the
rest of the run (PRD §16).
"""

from __future__ import annotations

from collections.abc import Sequence


class JobMonitorError(Exception):
    """Base class for every error this project raises deliberately."""


# ----------------------------------------------------------------- transport


class TransientError(JobMonitorError):
    """A failure that may succeed on a later attempt."""


class NetworkError(TransientError):
    """Timeout / connection reset / DNS failure."""


class ServerError(TransientError):
    """HTTP 5xx."""

    def __init__(self, message: str, *, status: int) -> None:
        super().__init__(message)
        self.status = status


class RateLimited(TransientError):
    """HTTP 429, with the server's requested delay when it supplied one."""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class PermanentError(JobMonitorError):
    """A failure that will not be fixed by retrying."""


class HttpStatusError(PermanentError):
    """A non-retryable HTTP status (typically 4xx other than 429)."""

    def __init__(self, message: str, *, status: int) -> None:
        super().__init__(message)
        self.status = status


class RetryBudgetExhausted(PermanentError):
    """All permitted attempts were made and every one failed transiently."""

    def __init__(self, message: str, *, attempts: int, last_error: Exception) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.last_error = last_error


class AccessBlocked(PermanentError):
    """The source deliberately refuses automated access (403, bot wall, CAPTCHA).

    Raised so the registry can be marked `blocked` rather than retried. This
    project never attempts to defeat such controls (PRD §5).
    """


# -------------------------------------------------------------------- parsing


class ParseError(PermanentError):
    """A source responded, but its payload was not the shape we can read."""


class InvalidJobError(PermanentError):
    """A single record was missing required fields or had an unusable URL.

    Adapters catch this per record: one malformed posting must never discard the
    other postings in the same response (PRD §7).
    """


# ----------------------------------------------------------------- config/data


class ProviderConfigError(JobMonitorError):
    """A company's provider_config is missing keys its adapter requires."""


__all__: Sequence[str] = (
    "AccessBlocked",
    "HttpStatusError",
    "InvalidJobError",
    "JobMonitorError",
    "NetworkError",
    "ParseError",
    "PermanentError",
    "ProviderConfigError",
    "RateLimited",
    "RetryBudgetExhausted",
    "ServerError",
    "TransientError",
)
