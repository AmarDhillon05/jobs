"""HTTP for the Apply kit, and the Kit Lambda's handler.

===========================================  ======================================
``GET  /kit/{job_id}?t=``                    the kit page (HTML)
``POST /kit/{job_id}/draft?t=``              ``{question, current?, redraft?}``
``POST /kit/{job_id}/answers?t=``            ``{question, answer, save_to_library?}``
``GET  /kit/{job_id}/resume?t=``             302 to a 10-minute private link
===========================================  ======================================

Every route needs the job's signed token (:mod:`jobmonitor.apply.tokens`); a bad
or missing one is a plain 404, indistinguishable from an unknown job. This runs in
its own Lambda, separate from the jobs API, so that only this function can read
the user's profile, resume and Anthropic key.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Mapping, Sequence
from functools import cache
from typing import Any
from urllib.parse import quote, unquote

from jobmonitor.api.routes import Request, Response
from jobmonitor.apply import kit_page, tokens
from jobmonitor.apply.drafter import ClaudeDrafter, Drafter, DraftError, SampleDrafter
from jobmonitor.apply.profile import Profile, ProfileError
from jobmonitor.apply.service import DraftLimitReached, KitNotFound, KitService
from jobmonitor.apply.store import DynamoKitStore
from jobmonitor.config import Settings

logger = logging.getLogger(__name__)

PAGE_HEADERS: Mapping[str, str] = {
    "X-Robots-Tag": "noindex, nofollow",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}
RESUME_LINK_SECONDS = 600


def _error(status: int, code: str, message: str) -> Response:
    return Response(status, {"error": {"code": code, "message": message}}, dict(PAGE_HEADERS))


def _not_found() -> Response:
    return _error(404, "not_found", "no such kit")


class KitApp:
    def __init__(
        self,
        service: KitService,
        *,
        secret: str | None,
        resume_url: Callable[[], str | None] = lambda: None,
    ) -> None:
        self.service = service
        self.secret = secret
        self.resume_url = resume_url

    def handle(self, request: Request) -> Response:
        # Job ids contain ':' ("salesforce:JR1"), which arrives percent-encoded.
        parts = [unquote(p) for p in request.path.strip("/").split("/") if p]
        if len(parts) < 2 or parts[0] != "kit":
            return _not_found()
        job_id, action = parts[1], (parts[2] if len(parts) > 2 else "")
        if len(parts) > 3:
            return _not_found()
        token = request.query.get("t")
        if not tokens.verify(job_id, token, self.secret):
            return _not_found()
        method = request.method.upper()
        try:
            if method == "GET" and action == "":
                return self._page(job_id, token or "")
            if method == "GET" and action == "resume":
                return self._resume(job_id)
            if method == "POST" and action == "draft":
                return self._draft(job_id, request)
            if method == "POST" and action == "answers":
                return self._save(job_id, request)
        except KitNotFound:
            return _not_found()
        except (ValueError, json.JSONDecodeError) as exc:
            return _error(400, "bad_request", str(exc))
        return _error(405, "method_not_allowed", f"{method} {request.path}")

    # ----------------------------------------------------------------- routes
    def _page(self, job_id: str, token: str) -> Response:
        try:
            kit = self.service.build(job_id)
        except ProfileError as exc:
            return _error(500, "profile", f"profile.json is unusable: {exc}")
        page = kit_page.render(kit, token=token, base_path=f"/kit/{quote(job_id, safe='')}")
        return Response(200, page, dict(PAGE_HEADERS), content_type="text/html; charset=utf-8")

    def _resume(self, job_id: str) -> Response:
        self.service.job(job_id)
        url = self.resume_url()
        if not url:
            return _error(404, "no_resume", "no resume uploaded: run make apply-setup")
        return Response(302, None, {**PAGE_HEADERS, "Location": url})

    @staticmethod
    def _body(request: Request) -> dict[str, Any]:
        data = json.loads(request.body or "{}")
        if not isinstance(data, dict):
            raise ValueError("body must be a JSON object")
        return data

    def _draft(self, job_id: str, request: Request) -> Response:
        body = self._body(request)
        try:
            answer = self.service.draft(
                job_id,
                str(body.get("question") or ""),
                current=body.get("current") or None,
                redraft=bool(body.get("redraft")),
            )
        except DraftLimitReached as exc:
            return _error(429, "draft_limit", str(exc))
        except DraftError as exc:
            return _error(502, "draft_failed", str(exc))
        return Response(
            200,
            {"answer": answer.to_dict(), "label": kit_page.SOURCE_LABELS.get(answer.source, "")},
            dict(PAGE_HEADERS),
        )

    def _save(self, job_id: str, request: Request) -> Response:
        body = self._body(request)
        text = str(body.get("answer") or "")
        if len(text) > 20_000:
            raise ValueError("answer is too long")
        answer = self.service.save(
            job_id,
            str(body.get("question") or ""),
            text,
            to_library=bool(body.get("save_to_library")),
        )
        label = "saved for next time" if body.get("save_to_library") else "your answer"
        return Response(200, {"answer": answer.to_dict(), "label": label}, dict(PAGE_HEADERS))


# ------------------------------------------------------------------ Lambda


@cache
def _ssm_parameter(name: str) -> str | None:
    import boto3

    try:
        response = boto3.client("ssm").get_parameter(Name=name, WithDecryption=True)
    except Exception as exc:  # absent until `make apply-setup` has run
        logger.warning("kit: SSM parameter %s unavailable: %s", name, exc)
        return None
    return str(response["Parameter"]["Value"]) or None


def secret_from_env(env: Mapping[str, str] | None = None) -> str | None:
    """``KIT_SECRET`` directly (local), or the SSM parameter named by
    ``KIT_SECRET_PARAMETER`` (deployed)."""
    e = os.environ if env is None else env
    if e.get("KIT_SECRET"):
        return e["KIT_SECRET"]
    name = e.get("KIT_SECRET_PARAMETER")
    return _ssm_parameter(name) if name else None


class S3Files:
    """The user's profile and resume in the private apply bucket."""

    PROFILE_KEY = "profile.json"
    RESUME_KEY = "resume.pdf"

    def __init__(self, bucket: str, *, client: Any = None) -> None:
        import boto3

        self.bucket = bucket
        self.client = client or boto3.client("s3")

    def _get(self, key: str) -> bytes | None:
        try:
            body: bytes = self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except Exception as exc:
            logger.warning("kit: s3://%s/%s unavailable: %s", self.bucket, key, exc)
            return None
        return body

    def profile(self) -> Profile:
        raw = self._get(self.PROFILE_KEY)
        return Profile.from_json(raw.decode("utf-8")) if raw else Profile()

    def resume(self) -> bytes | None:
        return self._get(self.RESUME_KEY)

    def has_resume(self) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=self.RESUME_KEY)
        except Exception:
            return False
        return True

    def resume_url(self) -> str | None:
        if not self.has_resume():
            return None
        url: str = self.client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": self.bucket,
                "Key": self.RESUME_KEY,
                "ResponseContentDisposition": 'attachment; filename="resume.pdf"',
            },
            ExpiresIn=RESUME_LINK_SECONDS,
        )
        return url


def build_app(env: Mapping[str, str] | None = None) -> KitApp:
    e = os.environ if env is None else env
    settings = Settings.from_env(e)
    from jobmonitor.http import HttpClient
    from jobmonitor.models.company import load_default_registry
    from jobmonitor.storage.dynamo import DynamoJobRepository

    s3_kwargs: dict[str, Any] = {"region_name": settings.aws.region}
    if settings.aws.endpoint_url:  # LocalStack
        s3_kwargs["endpoint_url"] = settings.aws.endpoint_url
    import boto3

    files = S3Files(settings.aws.apply_bucket or "", client=boto3.client("s3", **s3_kwargs))
    profile_cache: dict[str, Profile] = {}

    def profile() -> Profile:
        if "p" not in profile_cache:
            profile_cache["p"] = files.profile()
        return profile_cache["p"]

    def drafter() -> Drafter:
        if e.get("KIT_DRAFTER") == "sample":
            # LocalStack: no Anthropic key and no layer there (its runtime is x86).
            return SampleDrafter()
        key = e.get("ANTHROPIC_API_KEY") or (
            _ssm_parameter(e["ANTHROPIC_KEY_PARAMETER"])
            if e.get("ANTHROPIC_KEY_PARAMETER")
            else None
        )
        if not key:
            raise DraftError("no Anthropic API key: run make apply-setup with ANTHROPIC_API_KEY")
        resume = files.resume()
        if not resume:
            raise DraftError("no resume uploaded: run make apply-setup")
        return ClaudeDrafter(api_key=key, resume_pdf=resume, profile_summary=profile().summary())

    service = KitService(
        repository=DynamoJobRepository(settings.aws),
        store=DynamoKitStore.from_settings(settings.aws),
        profile=profile,
        drafter=drafter,
        registry=load_default_registry(),
        http=HttpClient(settings.http),
        resume_available=files.has_resume,
        draft_daily_limit=int(e.get("DRAFT_DAILY_LIMIT") or 60),
    )
    return KitApp(service, secret=secret_from_env(e), resume_url=files.resume_url)


_CACHED: KitApp | None = None


def handler(
    event: Mapping[str, Any] | None = None, context: Any = None, *, app: KitApp | None = None
) -> dict[str, Any]:
    from jobmonitor.api.lambda_handler import to_request

    global _CACHED
    if app is None:
        if _CACHED is None:
            _CACHED = build_app()
        app = _CACHED
    response = app.handle(to_request(event or {}))
    result: dict[str, Any] = {
        "statusCode": response.status,
        "headers": response.all_headers,
        "isBase64Encoded": False,
    }
    if response.status not in (204, 302):
        result["body"] = response.payload()
    return result


__all__: Sequence[str] = (
    "PAGE_HEADERS",
    "KitApp",
    "S3Files",
    "SampleDrafter",
    "build_app",
    "handler",
    "secret_from_env",
)
