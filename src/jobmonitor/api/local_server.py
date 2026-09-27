"""Run the jobs API locally with no AWS at all.

``make serve-api`` starts this against in-memory repositories (or LocalStack, if
``AWS_ENDPOINT_URL`` is set), so the PWA can be developed and its Playwright tests
run without any cloud dependency.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from jobmonitor.api.routes import Api, Request
from jobmonitor.config import Settings
from jobmonitor.models.company import load_default_registry
from jobmonitor.storage.base import JobRepository
from jobmonitor.storage.memory import (
    InMemoryDeviceRepository,
    InMemoryHealthRepository,
    InMemoryJobRepository,
)

logger = logging.getLogger(__name__)


def build_local_api(
    *, repository: JobRepository | None = None, write_token: str | None = None
) -> Api:
    settings = Settings.from_env()
    if settings.aws.is_local and repository is None:
        from jobmonitor.storage.dynamo import (
            DynamoDeviceRepository,
            DynamoHealthRepository,
            DynamoJobRepository,
        )

        aws = settings.aws
        return Api(
            repository=DynamoJobRepository(aws),
            health_repository=DynamoHealthRepository(aws),
            device_repository=DynamoDeviceRepository(aws),
            registry=load_default_registry(),
            write_token=write_token or os.environ.get("API_WRITE_TOKEN") or "local-dev-token",
        )
    return Api(
        repository=repository or InMemoryJobRepository(),
        health_repository=InMemoryHealthRepository(),
        device_repository=InMemoryDeviceRepository(),
        registry=load_default_registry(),
        write_token=write_token or os.environ.get("API_WRITE_TOKEN") or "local-dev-token",
    )


def make_request_handler(api: Api) -> type[BaseHTTPRequestHandler]:
    class LocalHandler(BaseHTTPRequestHandler):
        server_version = "jobmonitor-local/0.1"

        def _dispatch(self) -> None:
            parts = urlsplit(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length).decode("utf-8", "replace") if length else ""
            request = Request(
                method=self.command,
                path=parts.path,
                query={k: v[0] for k, v in parse_qs(parts.query).items()},
                headers=dict(self.headers.items()),
                body=body,
            )
            response = api.handle(request)
            payload = b"" if response.status == 204 else response.json().encode("utf-8")
            self.send_response(response.status)
            for key, value in response.all_headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if payload:
                self.wfile.write(payload)

        do_GET = _dispatch
        do_POST = _dispatch
        do_DELETE = _dispatch
        do_OPTIONS = _dispatch

        def log_message(self, fmt: str, *args: Any) -> None:
            logger.info("%s - %s", self.address_string(), fmt % args)

    return LocalHandler


def serve(
    api: Api, *, host: str = "127.0.0.1", port: int = 8000
) -> tuple[ThreadingHTTPServer, threading.Thread]:
    """Start the server on a background thread. Returns (server, thread)."""
    server = ThreadingHTTPServer((host, port), make_request_handler(api))
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="jobmonitor-api")
    thread.start()
    return server, thread


def seed_demo_data(repository: JobRepository) -> int:
    """Populate a few jobs so `make serve-app` shows something immediately."""
    from jobmonitor.models.job import Job

    demo = [
        ("Anthropic", "Software Engineer Intern", "San Francisco, CA", 88),
        ("Jane Street", "Software Engineer Internship", "New York, NY", 84),
        ("Databricks", "Machine Learning Engineer Intern", "Mountain View, CA", 81),
        ("Cloudflare", "Systems Engineer Intern", "Austin, TX", 78),
        ("Figma", "Backend Engineer Intern", "San Francisco, CA", 76),
    ]
    for index, (company, title, location, score) in enumerate(demo):
        repository.upsert(
            Job(
                company=company,
                title=title,
                url=f"https://example.test/{company.lower().replace(' ', '-')}/jobs/{index}",
                source="demo",
                external_id=str(index),
                location=location,
                description="Demo record created by the local API server.",
                employment_type="Intern",
            ),
            relevance_score=score,
        )
    return len(demo)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--seed", action="store_true", help="insert demo jobs on startup")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    api = build_local_api()
    if args.seed:
        count = seed_demo_data(api.repository)
        print(f"seeded {count} demo jobs")

    server = ThreadingHTTPServer((args.host, args.port), make_request_handler(api))
    print(f"jobs API listening on http://{args.host}:{args.port}")
    print(json.dumps({"endpoints": ["/jobs", "/jobs/{id}", "/health", "/meta"]}))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
