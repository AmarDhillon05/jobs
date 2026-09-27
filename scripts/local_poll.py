#!/usr/bin/env python3
"""Inject one scheduled-poll event into the local architecture.

This is the *simulated boundary* the docs refer to. LocalStack's community
EventBridge-scheduler emulation is unreliable, so following PRD §18.4 the
EventBridge-compatible rule stays in the CDK stack (asserted by
infrastructure/tests) and the trigger is reproduced here by delivering the
identical payload the rule would send. Everything downstream - queue, workers,
storage, fan-out, notifier - is real.

    python scripts/local_poll.py                            # poll everything
    python scripts/local_poll.py --company Anthropic        # just one
    python scripts/local_poll.py --provider ashby --dry-run
    python scripts/local_poll.py --watch                    # follow until drained
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from jobmonitor.orchestration.messages import ScheduledPollEvent  # noqa: E402
from local_provision import Resources, client  # noqa: E402

COORDINATOR = "jobmonitor-coordinator"


def queue_depth(sqs: object, url: str) -> int:
    attributes = sqs.get_queue_attributes(  # type: ignore[attr-defined]
        QueueUrl=url,
        AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
    )["Attributes"]
    return int(attributes["ApproximateNumberOfMessages"]) + int(
        attributes["ApproximateNumberOfMessagesNotVisible"]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:4566")
    parser.add_argument("--company", action="append", default=[], help="narrow to a company")
    parser.add_argument("--provider", help="narrow to one provider")
    parser.add_argument("--dry-run", action="store_true", help="shard but publish nothing")
    parser.add_argument("--watch", action="store_true", help="follow the queues until drained")
    parser.add_argument("--timeout", type=float, default=300.0)
    args = parser.parse_args(argv)

    resources = Resources.load()
    lam = client("lambda", resources.endpoint_url, resources.region)
    sqs = client("sqs", resources.endpoint_url, resources.region)

    event = ScheduledPollEvent(
        poll_id=f"local-{int(time.time())}",
        only_companies=tuple(args.company),
        only_provider=args.provider,
        dry_run=args.dry_run,
    )
    print(f"injecting the scheduled event the EventBridge rule would send:\n  {event.to_dict()}\n")

    response = lam.invoke(
        FunctionName=COORDINATOR, Payload=json.dumps({"detail": event.to_dict()}).encode()
    )
    body = response["Payload"].read().decode()
    if response.get("FunctionError"):
        print(f"coordinator failed: {body}", file=sys.stderr)
        return 1
    summary = json.loads(body) if body else {}
    print(f"coordinator: {json.dumps(summary)}")

    if not args.watch or args.dry_run:
        print("\nWorkers run asynchronously. Inspect results with:")
        print("  python -m jobmonitor.cli jobs   --url", resources.endpoint_url)
        print("  python -m jobmonitor.cli health --url", resources.endpoint_url)
        return 0

    print("\nwatching the queues...")
    deadline = time.monotonic() + args.timeout
    idle_ticks = 0
    while time.monotonic() < deadline:
        scrape = queue_depth(sqs, resources.scrape_queue_url)
        notify = queue_depth(sqs, resources.notification_queue_url)
        dead = queue_depth(sqs, resources.scrape_dlq_url) + queue_depth(
            sqs, resources.notification_dlq_url
        )
        print(f"  scrape={scrape:<4} notify={notify:<4} dlq={dead}")
        if scrape == 0 and notify == 0:
            idle_ticks += 1
            # Two consecutive idle reads: SQS counts are eventually consistent, so
            # one zero does not mean the run is over.
            if idle_ticks >= 2:
                print("\nqueues drained")
                if dead:
                    print(f"WARNING: {dead} message(s) in a dead-letter queue")
                return 0
        else:
            idle_ticks = 0
        time.sleep(3)

    print("\ntimed out waiting for the queues to drain", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
