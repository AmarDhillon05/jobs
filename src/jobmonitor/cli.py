"""Developer CLI: run a poll, inspect jobs, inspect scraper health (PRD §25).

    python -m jobmonitor.cli health                    # the health view
    python -m jobmonitor.cli jobs --limit 20
    python -m jobmonitor.cli poll --company Anthropic  # in-process, no AWS
    python -m jobmonitor.cli coverage                  # registry coverage
    python -m jobmonitor.cli devices                   # registered push targets
    python -m jobmonitor.cli push-test                 # one alert to every device

Without ``--url`` the commands run entirely in process against in-memory storage,
so `poll` works with no AWS and no LocalStack at all. With ``--url`` they read the
emulated (or real) DynamoDB tables the deployed stack writes.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime

from jobmonitor.config import Settings
from jobmonitor.filtering import JobFilter
from jobmonitor.models.company import SupportStatus, load_default_registry
from jobmonitor.models.health import ScraperHealth, ScraperStatus
from jobmonitor.notifications import Notifier, build_email_transport, build_push_transport
from jobmonitor.orchestration import PollRunner
from jobmonitor.storage.base import DeviceRepository, HealthRepository, JobRepository

STATUS_MARK = {
    ScraperStatus.SUCCESS: "OK",
    ScraperStatus.EMPTY: "EMPTY",
    ScraperStatus.DEGRADED: "DEGRADED",
    ScraperStatus.FAILED: "FAIL",
    ScraperStatus.BLOCKED: "BLOCKED",
}


def _age(when: datetime) -> str:
    seconds = int((datetime.now(UTC) - when).total_seconds())
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _repositories(url: str | None) -> tuple[JobRepository, HealthRepository]:
    if url:
        import os

        os.environ["AWS_ENDPOINT_URL"] = url
        os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
        os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")
        from jobmonitor.storage.dynamo import DynamoHealthRepository, DynamoJobRepository

        aws = Settings.from_env().aws
        return DynamoJobRepository(aws), DynamoHealthRepository(aws)

    from jobmonitor.storage.memory import InMemoryHealthRepository, InMemoryJobRepository

    return InMemoryJobRepository(), InMemoryHealthRepository()


def _device_repository(url: str | None) -> DeviceRepository:
    if url:
        import os

        os.environ["AWS_ENDPOINT_URL"] = url
        os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
        os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")
        from jobmonitor.storage.dynamo import DynamoDeviceRepository

        return DynamoDeviceRepository(Settings.from_env().aws)

    from jobmonitor.storage.memory import InMemoryDeviceRepository

    return InMemoryDeviceRepository()


# --------------------------------------------------------------------- commands


def cmd_health(args: argparse.Namespace) -> int:
    """The PRD §25 health view."""
    _jobs, health = _repositories(args.url)
    records: list[ScraperHealth] = health.latest()
    if not records:
        print("no health records yet - run a poll first")
        return 0

    if args.json:
        print(json.dumps([record.to_dict() for record in records], indent=2))
        return 0

    width = max(len(record.company) for record in records)
    failing = 0
    for record in sorted(records, key=lambda r: (r.ok, r.company)):
        mark = STATUS_MARK.get(record.status, record.status.value.upper())
        detail = ""
        if not record.ok:
            failing += 1
            detail = f"  {record.error_type or ''}: {(record.error or '')[:70]}"
        elif record.jobs_found:
            detail = f"  {record.jobs_found} found, {record.new_jobs} new"
        print(f"{record.company:<{width}}  {mark:<8}  {_age(record.timestamp):<9}{detail}")
    print(f"\n{len(records)} scraper(s), {failing} failing")
    return 1 if failing and args.strict else 0


def cmd_jobs(args: argparse.Namespace) -> int:
    jobs, _health = _repositories(args.url)
    records = jobs.recent(limit=args.limit)
    if args.json:
        print(json.dumps([record.to_api_dict() for record in records], indent=2))
        return 0
    if not records:
        print("no jobs stored yet")
        return 0
    for record in records:
        flag = "" if record.notification_sent else "  [not yet notified]"
        print(f"{record.relevance_score:>3}  {record.company} - {record.title}")
        print(f"     {record.location or 'location unknown'}  |  {_age(record.first_seen)}{flag}")
        print(f"     {record.url}")
    print(f"\n{len(records)} job(s)")
    return 0


def cmd_poll(args: argparse.Namespace) -> int:
    """Run a poll in process. Hits real careers endpoints unless narrowed."""
    settings = Settings.from_env()
    jobs, health = _repositories(args.url)
    registry = load_default_registry()

    targets = list(registry.pollable())
    if args.company:
        wanted = {name.casefold() for name in args.company}
        targets = [c for c in targets if c.company.casefold() in wanted]
    if args.provider:
        targets = [c for c in targets if c.provider == args.provider]
    if args.limit:
        targets = targets[: args.limit]
    if not targets:
        print("no companies matched the filters", file=sys.stderr)
        return 2

    notifier = Notifier(
        settings,
        repository=jobs,
        email_transport=build_email_transport(settings.email),
        push_transport=build_push_transport(settings.push),
    )
    runner = PollRunner(
        settings,
        repository=jobs,
        notifier=notifier,
        health_repository=health,
        job_filter=JobFilter(settings.filters),
    )
    print(f"polling {len(targets)} company/companies...")
    outcome = runner.run(targets, poll_id="cli")
    print(json.dumps(outcome.to_dict(), indent=2))
    for failure in outcome.failures:
        print(f"  FAILED {failure.company}: {failure.health.error}", file=sys.stderr)
    return 0


def cmd_devices(args: argparse.Namespace) -> int:
    """List the push targets that registered themselves (PRD §24)."""
    devices = _device_repository(args.url).enabled_devices()
    if args.json:
        print(json.dumps([device.to_item() for device in devices], indent=2))
        return 0
    if not devices:
        print("no devices registered yet - open the app and grant notification permission")
        return 0
    for device in devices:
        # Tokens are long and are credentials of a sort: show enough to identify
        # which handset this is, not enough to paste somewhere by accident.
        token = device.token
        shown = f"{token[:22]}...{token[-6:]}" if len(token) > 34 else token
        print(f"{device.device_id:<32}  {device.transport:<8}  {shown}")
    print(f"\n{len(devices)} device(s)")
    return 0


def cmd_push_test(args: argparse.Namespace) -> int:
    """Send one alert through the configured push transport.

    The point of this command is to isolate the last hop: if a real job never
    reaches the phone, this answers "is it the pipeline or the delivery?".
    """
    from jobmonitor.notifications import NotificationEvent, format_push
    from jobmonitor.notifications.transports import DeliveryError, TransportUnavailable

    settings = Settings.from_env()
    jobs, _health = _repositories(args.url)
    device_repository = _device_repository(args.url)
    devices = device_repository.enabled_devices()
    if not devices and settings.push.transport in {"expo", "webpush"}:
        print(
            f"no devices registered, and PUSH_TRANSPORT={settings.push.transport} needs one "
            "- open the app and grant notification permission first",
            file=sys.stderr,
        )
        return 2

    records = jobs.recent(limit=1)
    if not records:
        print(
            "no jobs stored yet - run a poll first, or use --url to read the deployed table",
            file=sys.stderr,
        )
        return 2

    event = NotificationEvent.for_records(records[:1], app_base_url=settings.app_base_url)
    transport = build_push_transport(
        settings.push,
        topic_arn=settings.aws.notification_topic_arn,
        region=settings.aws.region,
        endpoint_url=settings.aws.endpoint_url,
        device_repository=device_repository,
    )
    message = format_push(event)
    print(f"sending via {transport.name} to {len(devices)} device(s)")
    print(f"  title:     {message.title}")
    print(f"  deep_link: {message.deep_link}")
    try:
        receipts = transport.send(message, devices=devices)
    except (DeliveryError, TransportUnavailable) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    if not receipts:
        print("delivered to 0 devices - none of them use this transport")
        return 1
    print(f"accepted: {', '.join(receipts)}")
    print("\nTap the notification on the device: it must open the deep link printed above.")
    return 0


def cmd_coverage(args: argparse.Namespace) -> int:
    registry = load_default_registry()
    counts = registry.counts_by_status()
    print(f"Total companies in the registry: {len(registry)}")
    print(f"Monitored (pollable):           {len(registry.pollable())}")
    print()
    for status in SupportStatus:
        if counts[status.value]:
            print(f"  {status.value:<18} {counts[status.value]}")
    print("\nBy provider:")
    for provider, count in registry.counts_by_provider().items():
        print(f"  {provider:<20} {count}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jobmon", description=__doc__)
    parser.add_argument("--url", help="AWS endpoint URL (e.g. LocalStack at :4566)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    health = subparsers.add_parser("health", help="scraper health view")
    health.add_argument("--json", action="store_true")
    health.add_argument("--strict", action="store_true", help="exit non-zero if any scraper fails")
    health.set_defaults(func=cmd_health)

    jobs = subparsers.add_parser("jobs", help="recently discovered jobs")
    jobs.add_argument("--limit", type=int, default=25)
    jobs.add_argument("--json", action="store_true")
    jobs.set_defaults(func=cmd_jobs)

    poll = subparsers.add_parser("poll", help="run a poll in process")
    poll.add_argument("--company", action="append", default=[])
    poll.add_argument("--provider")
    poll.add_argument("--limit", type=int)
    poll.set_defaults(func=cmd_poll)

    coverage = subparsers.add_parser("coverage", help="registry coverage summary")
    coverage.set_defaults(func=cmd_coverage)

    devices = subparsers.add_parser("devices", help="registered push targets")
    devices.add_argument("--json", action="store_true")
    devices.set_defaults(func=cmd_devices)

    push_test = subparsers.add_parser(
        "push-test", help="send one alert through the configured push transport"
    )
    push_test.set_defaults(func=cmd_push_test)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
