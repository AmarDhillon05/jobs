"""Central configuration.

Everything the system needs is read from the process environment exactly once,
into an immutable :class:`Settings` object. Nothing else in the codebase reads
``os.environ`` directly, which is what makes the whole pipeline trivially
configurable from tests (pass a ``Settings`` in) and from Lambda (read the env).

No secret ever has a real default. See ``.env.example``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Final

Env = Mapping[str, str]

DEFAULT_USER_AGENT: Final = (
    "internship-job-monitor/0.1 (+personal job alerting; contact: you@example.com)"
)

_TRUTHY: Final = frozenset({"1", "true", "yes", "on", "y"})
_FALSEY: Final = frozenset({"0", "false", "no", "off", "n", ""})


class ConfigError(ValueError):
    """Raised when an environment value cannot be interpreted."""


def _str(env: Env, key: str, default: str) -> str:
    value = env.get(key)
    return default if value is None or value == "" else value


def _opt_str(env: Env, key: str) -> str | None:
    value = env.get(key)
    return None if value is None or value.strip() == "" else value.strip()


def _int(env: Env, key: str, default: int, *, minimum: int | None = None) -> int:
    raw = env.get(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer, got {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"{key} must be >= {minimum}, got {value}")
    return value


def _float(env: Env, key: str, default: float, *, minimum: float | None = None) -> float:
    raw = env.get(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{key} must be a number, got {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"{key} must be >= {minimum}, got {value}")
    return value


def _bool(env: Env, key: str, default: bool) -> bool:
    raw = env.get(key)
    if raw is None:
        return default
    lowered = raw.strip().lower()
    if lowered in _TRUTHY:
        return True
    if lowered in _FALSEY:
        return False
    raise ConfigError(f"{key} must be a boolean-ish value, got {raw!r}")


def _score(env: Env, key: str, default: int) -> int:
    value = _int(env, key, default)
    if not 0 <= value <= 100:
        raise ConfigError(f"{key} must be between 0 and 100, got {value}")
    return value


def _csv(env: Env, key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = env.get(key)
    if raw is None or raw.strip() == "":
        return default
    return tuple(part.strip() for part in raw.split(",") if part.strip())


@dataclass(frozen=True, slots=True)
class HttpSettings:
    timeout_seconds: float = 15.0
    max_attempts: int = 3
    backoff_base_seconds: float = 0.5
    backoff_max_seconds: float = 20.0
    user_agent: str = DEFAULT_USER_AGENT


@dataclass(frozen=True, slots=True)
class FilterSettings:
    """Two thresholds, deliberately.

    ``keep_threshold`` is what gets persisted; ``notify_threshold`` is what
    reaches the user. PRD §2 says to prefer retaining a low-scoring job over
    silently discarding it, so ``keep`` sits well below ``notify``.
    """

    notify_threshold: int = 55
    keep_threshold: int = 35
    immediate_alert_priorities: tuple[str, ...] = ("high",)

    def __post_init__(self) -> None:
        if self.keep_threshold > self.notify_threshold:
            raise ConfigError(
                "KEEP_RELEVANCE_THRESHOLD must be <= NOTIFY_RELEVANCE_THRESHOLD "
                f"({self.keep_threshold} > {self.notify_threshold})"
            )


@dataclass(frozen=True, slots=True)
class AwsSettings:
    region: str = "us-east-1"
    endpoint_url: str | None = None
    jobs_table: str = "jobmonitor-jobs"
    health_table: str = "jobmonitor-scraper-health"
    devices_table: str = "jobmonitor-devices"
    scrape_queue_url: str | None = None
    scrape_dlq_url: str | None = None
    notification_topic_arn: str | None = None
    notification_queue_url: str | None = None
    notification_dlq_url: str | None = None

    @property
    def is_local(self) -> bool:
        """True when pointed at LocalStack rather than real AWS."""
        return bool(self.endpoint_url)


@dataclass(frozen=True, slots=True)
class EmailSettings:
    transport: str = "console"
    sender: str = "alerts@example.com"
    recipient: str = "you@example.com"
    configuration_set: str | None = None


@dataclass(frozen=True, slots=True)
class PushSettings:
    transport: str = "console"
    vapid_public_key: str | None = None
    vapid_private_key: str | None = None
    vapid_subject: str = "mailto:you@example.com"
    #: Expo push service. The access token is only required when the Expo project
    #: has "enhanced security for push notifications" switched on.
    expo_api_url: str = "https://exp.host/--/api/v2/push/send"
    expo_access_token: str | None = None


@dataclass(frozen=True, slots=True)
class Settings:
    aws: AwsSettings = field(default_factory=AwsSettings)
    http: HttpSettings = field(default_factory=HttpSettings)
    filters: FilterSettings = field(default_factory=FilterSettings)
    email: EmailSettings = field(default_factory=EmailSettings)
    push: PushSettings = field(default_factory=PushSettings)
    poll_interval_minutes: int = 10
    shard_size: int = 8
    app_base_url: str = "http://localhost:5173"
    api_base_url: str = "http://localhost:8000"
    enable_live_tests: bool = False

    # ---------------------------------------------------------------- loading
    @classmethod
    def from_env(cls, env: Env | None = None) -> Settings:
        e: Env = os.environ if env is None else env
        return cls(
            aws=AwsSettings(
                region=_str(e, "AWS_REGION", "us-east-1"),
                endpoint_url=_opt_str(e, "AWS_ENDPOINT_URL"),
                jobs_table=_str(e, "JOBS_TABLE_NAME", "jobmonitor-jobs"),
                health_table=_str(e, "HEALTH_TABLE_NAME", "jobmonitor-scraper-health"),
                devices_table=_str(e, "DEVICES_TABLE_NAME", "jobmonitor-devices"),
                scrape_queue_url=_opt_str(e, "SCRAPE_QUEUE_URL"),
                scrape_dlq_url=_opt_str(e, "SCRAPE_DLQ_URL"),
                notification_topic_arn=_opt_str(e, "NOTIFICATION_TOPIC_ARN"),
                notification_queue_url=_opt_str(e, "NOTIFICATION_QUEUE_URL"),
                notification_dlq_url=_opt_str(e, "NOTIFICATION_DLQ_URL"),
            ),
            http=HttpSettings(
                timeout_seconds=_float(e, "HTTP_TIMEOUT_SECONDS", 15.0, minimum=0.1),
                max_attempts=_int(e, "HTTP_MAX_ATTEMPTS", 3, minimum=1),
                backoff_base_seconds=_float(e, "HTTP_BACKOFF_BASE_SECONDS", 0.5, minimum=0.0),
                backoff_max_seconds=_float(e, "HTTP_BACKOFF_MAX_SECONDS", 20.0, minimum=0.0),
                user_agent=_str(e, "HTTP_USER_AGENT", DEFAULT_USER_AGENT),
            ),
            filters=FilterSettings(
                notify_threshold=_score(e, "NOTIFY_RELEVANCE_THRESHOLD", 55),
                keep_threshold=_score(e, "KEEP_RELEVANCE_THRESHOLD", 35),
                immediate_alert_priorities=_csv(e, "IMMEDIATE_ALERT_PRIORITIES", ("high",)),
            ),
            email=EmailSettings(
                transport=_str(e, "EMAIL_TRANSPORT", "console").lower(),
                sender=_str(e, "EMAIL_FROM", "alerts@example.com"),
                recipient=_str(e, "EMAIL_TO", "you@example.com"),
                configuration_set=_opt_str(e, "SES_CONFIGURATION_SET"),
            ),
            push=PushSettings(
                transport=_str(e, "PUSH_TRANSPORT", "console").lower(),
                vapid_public_key=_opt_str(e, "VAPID_PUBLIC_KEY"),
                vapid_private_key=_opt_str(e, "VAPID_PRIVATE_KEY"),
                vapid_subject=_str(e, "VAPID_SUBJECT", "mailto:you@example.com"),
                expo_api_url=_str(e, "EXPO_PUSH_API_URL", "https://exp.host/--/api/v2/push/send"),
                expo_access_token=_opt_str(e, "EXPO_ACCESS_TOKEN"),
            ),
            poll_interval_minutes=_int(e, "POLL_INTERVAL_MINUTES", 10, minimum=1),
            shard_size=_int(e, "SHARD_SIZE", 8, minimum=1),
            app_base_url=_str(e, "APP_BASE_URL", "http://localhost:5173").rstrip("/"),
            api_base_url=_str(e, "API_BASE_URL", "http://localhost:8000").rstrip("/"),
            enable_live_tests=_bool(e, "ENABLE_LIVE_TESTS", False),
        )

    def with_overrides(self, **kwargs: object) -> Settings:
        """Return a copy with top-level fields replaced (test convenience)."""
        return replace(self, **kwargs)  # type: ignore[arg-type]


def for_tests(**overrides: object) -> Settings:
    """Deterministic settings for automated tests: no network, no real secrets."""
    base = Settings(
        aws=AwsSettings(region="us-east-1", endpoint_url=None),
        http=HttpSettings(
            timeout_seconds=1.0,
            max_attempts=3,
            backoff_base_seconds=0.0,
            backoff_max_seconds=0.0,
        ),
        email=EmailSettings(transport="memory"),
        push=PushSettings(transport="memory"),
        app_base_url="https://app.test",
        api_base_url="https://api.test",
    )
    return base.with_overrides(**overrides) if overrides else base
