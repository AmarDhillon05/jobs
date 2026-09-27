"""The jobs API (PRD §24), independent of any web framework.

Routing works on a normalized :class:`Request` and returns a :class:`Response`, so
exactly the same code serves API Gateway (``lambda_handler.py``) and a local
stdlib HTTP server (``local_server.py``) - and is directly unit-testable without
either.

Endpoints:

===============================  ====================================
``GET  /jobs``                   recent jobs, filterable
``GET  /jobs/recent``            alias, for PRD §24 compatibility
``GET  /jobs/{id}``              one job (the notification deep-link target)
``POST /devices/register``       register a push target
``DELETE /devices/{id}``         unregister
``GET  /health``                 scraper health view (PRD §25)
``GET  /meta``                   registry/coverage counts
===============================  ====================================

Read endpoints are open (the data is public job postings, and the client is a
static PWA). Writes are protected by a shared token, checked in constant time -
see :func:`require_write_token`. No AWS credential ever reaches the client.
"""

from __future__ import annotations

import hmac
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import unquote

from jobmonitor.models.company import CompanyRegistry
from jobmonitor.models.record import DeviceRegistration, utcnow
from jobmonitor.storage.base import DeviceRepository, HealthRepository, JobRepository

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
CORS_HEADERS: Mapping[str, str] = {
    # The PWA is served from a different origin to the API.
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, X-Api-Token",
    "Access-Control-Max-Age": "600",
}


@dataclass(frozen=True, slots=True)
class Request:
    method: str
    path: str
    query: Mapping[str, str] = field(default_factory=dict)
    headers: Mapping[str, str] = field(default_factory=dict)
    body: str = ""

    def header(self, name: str) -> str | None:
        lowered = name.lower()
        for key, value in self.headers.items():
            if key.lower() == lowered:
                return value
        return None

    def json_body(self) -> Any:
        if not self.body.strip():
            return None
        return json.loads(self.body)

    def int_param(self, name: str, default: int, *, maximum: int | None = None) -> int:
        raw = self.query.get(name)
        if raw is None or raw == "":
            return default
        try:
            value = int(raw)
        except ValueError as exc:
            raise BadRequest(f"{name} must be an integer, got {raw!r}") from exc
        if value < 1:
            raise BadRequest(f"{name} must be >= 1, got {value}")
        if maximum is not None:
            value = min(value, maximum)
        return value


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Any = None
    headers: Mapping[str, str] = field(default_factory=dict)

    def json(self) -> str:
        return json.dumps(self.body if self.body is not None else {})

    @property
    def all_headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
            **CORS_HEADERS,
            **self.headers,
        }


class ApiError(Exception):
    status = 500
    code = "internal_error"


class BadRequest(ApiError):
    status = 400
    code = "bad_request"


class Unauthorized(ApiError):
    status = 401
    code = "unauthorized"


class NotFound(ApiError):
    status = 404
    code = "not_found"


def error_response(exc: ApiError) -> Response:
    return Response(exc.status, {"error": {"code": exc.code, "message": str(exc)}})


# ----------------------------------------------------------------------- router

Handler = Callable[["Api", Request, dict[str, str]], Response]

_ROUTES: list[tuple[str, re.Pattern[str], Handler]] = []


def route(method: str, pattern: str) -> Callable[[Handler], Handler]:
    """Register a handler. ``{name}`` in the pattern becomes a path parameter."""
    regex = re.compile(
        "^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern.rstrip("/") or "/") + "/?$"
    )

    def decorator(handler: Handler) -> Handler:
        _ROUTES.append((method.upper(), regex, handler))
        return handler

    return decorator


class Api:
    def __init__(
        self,
        *,
        repository: JobRepository,
        health_repository: HealthRepository | None = None,
        device_repository: DeviceRepository | None = None,
        registry: CompanyRegistry | None = None,
        write_token: str | None = None,
    ) -> None:
        self.repository = repository
        self.health_repository = health_repository
        self.device_repository = device_repository
        self.registry = registry
        self.write_token = write_token

    # --------------------------------------------------------------- dispatch
    def handle(self, request: Request) -> Response:
        if request.method.upper() == "OPTIONS":
            return Response(204, None)

        normalized = "/" + request.path.strip("/") if request.path.strip("/") else "/"
        for method, regex, handler in _ROUTES:
            match = regex.match(normalized)
            if not match:
                continue
            if method != request.method.upper():
                continue
            params = {key: unquote(value) for key, value in match.groupdict().items()}
            try:
                return handler(self, request, params)
            except ApiError as exc:
                return error_response(exc)
            except json.JSONDecodeError as exc:
                return error_response(BadRequest(f"body is not valid JSON: {exc}"))

        # Distinguish "wrong method" from "no such path" - it is the difference
        # between a client bug and a typo.
        if any(regex.match(normalized) for _method, regex, _handler in _ROUTES):
            return Response(
                405, {"error": {"code": "method_not_allowed", "message": request.method}}
            )
        return error_response(NotFound(f"no route for {request.method} {request.path}"))

    def require_write_token(self, request: Request) -> None:
        """Constant-time shared-token check for write endpoints."""
        if not self.write_token:
            raise Unauthorized("write endpoints are disabled: API_WRITE_TOKEN is not configured")
        supplied = request.header("X-Api-Token") or ""
        if not hmac.compare_digest(supplied, self.write_token):
            raise Unauthorized("invalid or missing X-Api-Token")


# ---------------------------------------------------------------------- routes


@route("GET", "/")
def index(api: Api, request: Request, params: dict[str, str]) -> Response:
    return Response(
        200,
        {
            "service": "internship-job-monitor",
            "endpoints": [
                "GET /jobs",
                "GET /jobs/recent",
                "GET /jobs/{id}",
                "POST /devices/register",
                "DELETE /devices/{id}",
                "GET /health",
                "GET /meta",
            ],
        },
    )


def _list_jobs(api: Api, request: Request) -> Response:
    limit = request.int_param("limit", DEFAULT_LIMIT, maximum=MAX_LIMIT)
    since: datetime | None = None
    if hours := request.query.get("hours"):
        try:
            since = utcnow() - timedelta(hours=float(hours))
        except ValueError as exc:
            raise BadRequest(f"hours must be a number, got {hours!r}") from exc
    elif raw_since := request.query.get("since"):
        try:
            since = datetime.fromisoformat(raw_since.replace("Z", "+00:00"))
        except ValueError as exc:
            raise BadRequest(f"since must be an ISO-8601 timestamp, got {raw_since!r}") from exc
        if since.tzinfo is None:
            since = since.replace(tzinfo=UTC)

    if company := request.query.get("company"):
        records = api.repository.by_company(company, limit=limit)
    else:
        records = api.repository.recent(limit=limit, since=since)

    if min_score := request.query.get("min_score"):
        try:
            threshold = int(min_score)
        except ValueError as exc:
            raise BadRequest(f"min_score must be an integer, got {min_score!r}") from exc
        records = [record for record in records if record.relevance_score >= threshold]

    return Response(
        200,
        {
            "count": len(records),
            "limit": limit,
            "jobs": [record.to_api_dict() for record in records],
        },
    )


@route("GET", "/jobs")
def list_jobs(api: Api, request: Request, params: dict[str, str]) -> Response:
    return _list_jobs(api, request)


@route("GET", "/jobs/recent")
def list_recent_jobs(api: Api, request: Request, params: dict[str, str]) -> Response:
    """PRD §24 lists this separately; it is `/jobs` with the default ordering."""
    return _list_jobs(api, request)


@route("GET", "/jobs/{job_id}")
def get_job(api: Api, request: Request, params: dict[str, str]) -> Response:
    """The deep-link target: a notification tap resolves to exactly this."""
    job_id = params["job_id"]
    record = api.repository.get(job_id)
    if record is None:
        raise NotFound(f"no job with id {job_id!r}")
    return Response(200, {"job": record.to_api_dict()})


@route("POST", "/devices/register")
def register_device(api: Api, request: Request, params: dict[str, str]) -> Response:
    api.require_write_token(request)
    if api.device_repository is None:
        raise ApiError("device registry is not configured")

    payload = request.json_body()
    if not isinstance(payload, Mapping):
        raise BadRequest("expected a JSON object")
    device_id = str(payload.get("device_id") or "").strip()
    token = str(payload.get("token") or "").strip()
    if not device_id:
        raise BadRequest("device_id is required")
    if not token:
        raise BadRequest("token is required")

    now = utcnow()
    device = api.device_repository.register(
        DeviceRegistration(
            device_id=device_id,
            token=token,
            transport=str(payload.get("transport") or "webpush"),
            registered_at=now,
            last_seen=now,
            label=(str(payload["label"]) if payload.get("label") else None),
        )
    )
    # The token is a credential: acknowledge it, never echo it back.
    return Response(
        201,
        {
            "device": {
                "device_id": device.device_id,
                "transport": device.transport,
                "registered_at": device.registered_at.isoformat(),
                "label": device.label,
            }
        },
    )


@route("DELETE", "/devices/{device_id}")
def unregister_device(api: Api, request: Request, params: dict[str, str]) -> Response:
    api.require_write_token(request)
    if api.device_repository is None:
        raise ApiError("device registry is not configured")
    removed = api.device_repository.unregister(params["device_id"])
    if not removed:
        raise NotFound(f"no device with id {params['device_id']!r}")
    return Response(204, None)


@route("GET", "/health")
def health_view(api: Api, request: Request, params: dict[str, str]) -> Response:
    """Scraper health (PRD §25). Read-only diagnostics, no secrets."""
    if api.health_repository is None:
        return Response(200, {"scrapers": [], "note": "health tracking is not configured"})
    records = api.health_repository.latest()
    failures = [record for record in records if not record.ok]
    return Response(
        200,
        {
            "scrapers_total": len(records),
            "scrapers_failing": len(failures),
            "scrapers": [record.to_dict() for record in records],
        },
    )


@route("GET", "/meta")
def meta(api: Api, request: Request, params: dict[str, str]) -> Response:
    body: dict[str, Any] = {"jobs_stored": api.repository.count()}
    if api.registry is not None:
        body["companies"] = {
            "total": len(api.registry),
            "pollable": len(api.registry.pollable()),
            "by_status": api.registry.counts_by_status(),
            "by_provider": api.registry.counts_by_provider(),
        }
    return Response(200, body)


__all__: Sequence[str] = (
    "CORS_HEADERS",
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "Api",
    "ApiError",
    "BadRequest",
    "NotFound",
    "Request",
    "Response",
    "Unauthorized",
    "route",
)
