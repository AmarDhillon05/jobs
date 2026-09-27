#!/usr/bin/env python3
"""Block until LocalStack reports the services this project uses are available.

`docker compose up -d` returns as soon as the container starts, which is well
before DynamoDB or Lambda can answer. Polling the health endpoint - rather than
sleeping a guessed number of seconds - is what stops the LocalStack tests being
flaky for reasons that have nothing to do with the code under test.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

REQUIRED_SERVICES = ("dynamodb", "sqs", "sns", "lambda", "iam", "logs", "sts")
READY_STATES = {"available", "running"}


def probe(url: str, timeout: float = 5.0) -> dict[str, str]:
    request = urllib.request.Request(f"{url.rstrip('/')}/_localstack/health")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read())
    services = payload.get("services") or {}
    return {str(name): str(state) for name, state in services.items()}


def wait(url: str, *, timeout: float, interval: float = 2.0) -> dict[str, str]:
    deadline = time.monotonic() + timeout
    last: dict[str, str] = {}
    last_error: str | None = None

    while time.monotonic() < deadline:
        try:
            last = probe(url)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(interval)
            continue

        missing = [
            service
            for service in REQUIRED_SERVICES
            if last.get(service, "unknown") not in READY_STATES
        ]
        if not missing:
            return last
        time.sleep(interval)

    detail = json.dumps(last, indent=1) if last else f"never responded ({last_error})"
    raise SystemExit(
        f"LocalStack at {url} was not ready within {timeout:.0f}s.\n"
        f"Required: {', '.join(REQUIRED_SERVICES)}\nLast seen: {detail}\n"
        "Check `docker compose logs localstack`."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:4566")
    parser.add_argument("--timeout", type=float, default=240.0)
    args = parser.parse_args(argv)

    started = time.monotonic()
    services = wait(args.url, timeout=args.timeout)
    elapsed = time.monotonic() - started
    ready = sorted(name for name, state in services.items() if state in READY_STATES)
    print(f"LocalStack ready in {elapsed:.1f}s: {', '.join(ready)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
