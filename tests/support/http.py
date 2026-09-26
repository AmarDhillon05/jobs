"""A scripted HTTP transport.

Lets a test say exactly what the network does - ``500, 500, 200``, a ``429`` with
``Retry-After: 2``, a connection reset - with no mocking library, no sockets and
no real waiting, because :class:`~jobmonitor.http.HttpClient` takes its transport
and its ``sleep`` as constructor arguments.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jobmonitor.http import HttpRequest, HttpResponse

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def load_fixture(*parts: str) -> Any:
    """Load a saved provider response from ``tests/fixtures``."""
    path = FIXTURES.joinpath(*parts)
    if not path.exists():
        raise FileNotFoundError(f"fixture not found: {path}")
    text = path.read_text(encoding="utf-8")
    return json.loads(text) if path.suffix == ".json" else text


def load_fixture_bytes(*parts: str) -> bytes:
    return FIXTURES.joinpath(*parts).read_bytes()


@dataclass(frozen=True)
class ScriptedResponse:
    """One programmed reply: a response, or an exception to raise."""

    status: int = 200
    body: bytes = b""
    headers: Mapping[str, str] = field(default_factory=dict)
    raises: BaseException | None = None

    @classmethod
    def json(
        cls, payload: Any, *, status: int = 200, headers: Mapping[str, str] | None = None
    ) -> ScriptedResponse:
        return cls(
            status=status,
            body=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", **(headers or {})},
        )

    @classmethod
    def text(cls, body: str, *, status: int = 200) -> ScriptedResponse:
        return cls(status=status, body=body.encode("utf-8"))

    @classmethod
    def error(cls, status: int, *, headers: Mapping[str, str] | None = None) -> ScriptedResponse:
        return cls(status=status, body=b"", headers=headers or {})

    @classmethod
    def boom(cls, exc: BaseException) -> ScriptedResponse:
        return cls(raises=exc)


def json_response(payload: Any, **kwargs: Any) -> ScriptedResponse:
    return ScriptedResponse.json(payload, **kwargs)


class FakeTransport:
    """Replays a script of responses and records every request it received.

    * ``FakeTransport([r1, r2])`` - replies in order, then raises if asked again
      (an unexpected extra request is a test failure, not a silent repeat).
    * ``FakeTransport(always=r)`` - replies the same way forever.
    * ``FakeTransport(router=fn)`` - chooses the reply from the request, for
      pagination tests that depend on the URL.
    """

    def __init__(
        self,
        script: Iterable[ScriptedResponse] | None = None,
        *,
        always: ScriptedResponse | None = None,
        router: Callable[[HttpRequest], ScriptedResponse] | None = None,
    ) -> None:
        if sum(x is not None for x in (script, always, router)) != 1:
            raise ValueError("provide exactly one of script=, always= or router=")
        self._script: list[ScriptedResponse] = list(script or [])
        self._always = always
        self._router = router
        self.requests: list[HttpRequest] = []

    # ------------------------------------------------------------------ transport
    def __call__(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        scripted = self._next(request)
        if scripted.raises is not None:
            raise scripted.raises
        return HttpResponse(
            status=scripted.status,
            body=scripted.body,
            headers=dict(scripted.headers),
            url=request.url,
        )

    def _next(self, request: HttpRequest) -> ScriptedResponse:
        if self._router is not None:
            return self._router(request)
        if self._always is not None:
            return self._always
        if not self._script:
            raise AssertionError(
                f"FakeTransport script exhausted: unexpected {request.method} {request.url} "
                f"(already served {len(self.requests) - 1} request(s))"
            )
        return self._script.pop(0)

    # -------------------------------------------------------------- assertions
    @property
    def call_count(self) -> int:
        return len(self.requests)

    @property
    def urls(self) -> list[str]:
        return [request.url for request in self.requests]

    @property
    def last_request(self) -> HttpRequest:
        if not self.requests:
            raise AssertionError("no requests were made")
        return self.requests[-1]

    def bodies(self) -> list[Any]:
        return [json.loads(r.body) if r.body else None for r in self.requests]

    @property
    def remaining(self) -> int:
        return len(self._script)


class RecordingSleeper:
    """Captures backoff delays instead of waiting for them."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    def __call__(self, delay: float) -> None:
        self.delays.append(delay)

    @property
    def total(self) -> float:
        return sum(self.delays)


__all__: Sequence[str] = (
    "FIXTURES",
    "FakeTransport",
    "RecordingSleeper",
    "ScriptedResponse",
    "json_response",
    "load_fixture",
    "load_fixture_bytes",
)
