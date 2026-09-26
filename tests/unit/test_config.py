"""Level 1 - configuration loading (PRD §14 "config loading")."""

from __future__ import annotations

import pytest

from jobmonitor.config import ConfigError, Settings, for_tests


def test_defaults_match_documented_values() -> None:
    settings = Settings.from_env({})
    assert settings.poll_interval_minutes == 10
    assert settings.shard_size == 8
    assert settings.aws.region == "us-east-1"
    assert settings.aws.jobs_table == "jobmonitor-jobs"
    assert settings.filters.notify_threshold == 55
    assert settings.filters.keep_threshold == 35
    assert settings.filters.immediate_alert_priorities == ("high",)
    assert settings.http.max_attempts == 3
    assert settings.enable_live_tests is False


def test_env_values_override_defaults() -> None:
    settings = Settings.from_env(
        {
            "AWS_REGION": "eu-west-2",
            "AWS_ENDPOINT_URL": "http://localhost:4566",
            "JOBS_TABLE_NAME": "custom-jobs",
            "POLL_INTERVAL_MINUTES": "5",
            "SHARD_SIZE": "20",
            "NOTIFY_RELEVANCE_THRESHOLD": "70",
            "KEEP_RELEVANCE_THRESHOLD": "10",
            "IMMEDIATE_ALERT_PRIORITIES": "high, medium ,",
            "HTTP_MAX_ATTEMPTS": "5",
            "ENABLE_LIVE_TESTS": "yes",
        }
    )
    assert settings.aws.region == "eu-west-2"
    assert settings.aws.jobs_table == "custom-jobs"
    assert settings.poll_interval_minutes == 5
    assert settings.shard_size == 20
    assert settings.filters.notify_threshold == 70
    assert settings.filters.keep_threshold == 10
    assert settings.filters.immediate_alert_priorities == ("high", "medium")
    assert settings.http.max_attempts == 5
    assert settings.enable_live_tests is True


def test_blank_strings_fall_back_to_defaults() -> None:
    # A `.env` with `JOBS_TABLE_NAME=` must not produce an empty table name.
    settings = Settings.from_env({"JOBS_TABLE_NAME": "", "POLL_INTERVAL_MINUTES": "  "})
    assert settings.aws.jobs_table == "jobmonitor-jobs"
    assert settings.poll_interval_minutes == 10


def test_optional_values_become_none_when_blank() -> None:
    settings = Settings.from_env({"SCRAPE_QUEUE_URL": "   ", "NOTIFICATION_TOPIC_ARN": ""})
    assert settings.aws.scrape_queue_url is None
    assert settings.aws.notification_topic_arn is None


def test_is_local_tracks_endpoint_url() -> None:
    assert Settings.from_env({}).aws.is_local is False
    assert Settings.from_env({"AWS_ENDPOINT_URL": "http://localhost:4566"}).aws.is_local is True


def test_trailing_slashes_are_stripped_from_base_urls() -> None:
    settings = Settings.from_env(
        {"APP_BASE_URL": "https://app.example.com/", "API_BASE_URL": "https://api.example.com//"}
    )
    assert settings.app_base_url == "https://app.example.com"
    assert settings.api_base_url == "https://api.example.com"


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"POLL_INTERVAL_MINUTES": "ten"}, "must be an integer"),
        ({"POLL_INTERVAL_MINUTES": "0"}, "must be >= 1"),
        ({"HTTP_TIMEOUT_SECONDS": "abc"}, "must be a number"),
        ({"NOTIFY_RELEVANCE_THRESHOLD": "101"}, "between 0 and 100"),
        ({"ENABLE_LIVE_TESTS": "maybe"}, "boolean-ish"),
    ],
)
def test_invalid_values_raise_config_error(env: dict[str, str], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        Settings.from_env(env)


def test_keep_threshold_above_notify_threshold_is_rejected() -> None:
    with pytest.raises(ConfigError, match="must be <="):
        Settings.from_env({"KEEP_RELEVANCE_THRESHOLD": "90", "NOTIFY_RELEVANCE_THRESHOLD": "50"})


def test_settings_are_immutable() -> None:
    settings = Settings.from_env({})
    with pytest.raises((AttributeError, TypeError)):
        settings.shard_size = 99  # type: ignore[misc]


def test_for_tests_uses_no_network_and_fake_transports() -> None:
    settings = for_tests()
    assert settings.email.transport == "memory"
    assert settings.push.transport == "memory"
    assert settings.http.backoff_base_seconds == 0.0
    assert settings.push.vapid_private_key is None


def test_for_tests_accepts_overrides() -> None:
    settings = for_tests(shard_size=3)
    assert settings.shard_size == 3
    assert settings.email.transport == "memory"
