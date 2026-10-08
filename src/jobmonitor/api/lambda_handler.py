"""API Gateway HTTP API (payload v2) adapter.

A thin translation layer: event -> :class:`Request`, :class:`Response` -> result.
All the behaviour is in ``routes.py``, which is why the API is tested without
constructing a single API Gateway event.
"""

from __future__ import annotations

import base64
import os
from collections.abc import Mapping, Sequence
from typing import Any

from jobmonitor.api.routes import Api, Request
from jobmonitor.config import Settings
from jobmonitor.models.company import load_default_registry
from jobmonitor.storage.dynamo import (
    DynamoDeviceRepository,
    DynamoHealthRepository,
    DynamoJobRepository,
)

_CACHED_API: Api | None = None


def build_api() -> Api:
    settings = Settings.from_env()
    aws = settings.aws
    return Api(
        repository=DynamoJobRepository(aws),
        health_repository=DynamoHealthRepository(aws),
        device_repository=DynamoDeviceRepository(aws),
        registry=load_default_registry(),
        # Absent => write endpoints refuse, rather than defaulting to open.
        write_token=os.environ.get("API_WRITE_TOKEN") or None,
    )


def get_api(override: Api | None = None) -> Api:
    global _CACHED_API
    if override is not None:
        return override
    if _CACHED_API is None:
        _CACHED_API = build_api()
    return _CACHED_API


def reset_api() -> None:
    global _CACHED_API
    _CACHED_API = None


def to_request(event: Mapping[str, Any]) -> Request:
    """Translate an API Gateway v2 (or v1) event into a Request."""
    context = event.get("requestContext") or {}
    http = context.get("http") or {}
    method = str(http.get("method") or event.get("httpMethod") or "GET")
    path = str(http.get("path") or event.get("rawPath") or event.get("path") or "/")

    # Strip the stage prefix API Gateway prepends on non-$default stages.
    stage = str(context.get("stage") or "")
    if stage and stage != "$default" and path.startswith(f"/{stage}/"):
        path = path[len(stage) + 1 :]

    body = event.get("body") or ""
    if event.get("isBase64Encoded") and body:
        body = base64.b64decode(body).decode("utf-8", "replace")

    return Request(
        method=method,
        path=path,
        query={k: str(v) for k, v in (event.get("queryStringParameters") or {}).items()},
        headers={k: str(v) for k, v in (event.get("headers") or {}).items()},
        body=str(body),
    )


def handler(
    event: Mapping[str, Any] | None = None, context: Any = None, *, api: Api | None = None
) -> dict[str, Any]:
    response = get_api(api).handle(to_request(event or {}))
    result: dict[str, Any] = {
        "statusCode": response.status,
        "headers": response.all_headers,
        "isBase64Encoded": False,
    }
    if response.status != 204:
        result["body"] = response.payload()
    return result


__all__: Sequence[str] = ("build_api", "get_api", "handler", "reset_api", "to_request")
