#!/usr/bin/env python3
"""``make verify`` - the hard completion gate (PRD §14 Level 8).

Runs every check from a clean working state, in dependency order, and prints one
pass/fail report. Stages are ordered cheapest-first so an obvious break is
reported in seconds rather than after a LocalStack boot.

    make verify                      # everything
    make verify ARGS='--no-docker'   # skip the LocalStack stages
    make verify ARGS='--list'        # show the stages without running them

Exit status is 0 only if every stage that ran passed. Stages that are skipped for
a documented environmental reason (no Docker, no network) are reported as SKIP and
do not mask a failure elsewhere - but the summary says plainly that the run was
partial, because a green tick that quietly covered less than it claims is the
failure mode this whole file exists to prevent.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
VENV_PY = REPO_ROOT / ".venv" / "bin" / "python"
#: The two clients: the Expo/React Native phone app, and the browser feed.
MOBILE = REPO_ROOT / "mobile"
WEB = REPO_ROOT / "web"

GREEN, RED, YELLOW, GREY, BOLD, RESET = (
    "\033[32m",
    "\033[31m",
    "\033[33m",
    "\033[90m",
    "\033[1m",
    "\033[0m",
)


def colour(text: str, code: str) -> str:
    return text if os.environ.get("NO_COLOR") else f"{code}{text}{RESET}"


@dataclass
class Stage:
    key: str
    title: str
    command: list[str]
    cwd: Path = REPO_ROOT
    #: PRD gate(s) this stage provides evidence for.
    gate: str = ""
    needs_docker: bool = False
    needs_node: bool = False
    #: Stage keys that must have passed first.
    after: tuple[str, ...] = ()
    timeout: float = 3600.0


@dataclass
class Result:
    stage: Stage
    status: str  # pass | fail | skip
    seconds: float = 0.0
    detail: str = ""
    tail: list[str] = field(default_factory=list)


def stages() -> list[Stage]:
    py = str(VENV_PY)
    return [
        Stage(
            "lint",
            "Ruff lint",
            [py, "-m", "ruff", "check", "src", "infrastructure", "tests", "scripts"],
            gate="H",
        ),
        Stage(
            "format",
            "Ruff format check",
            [py, "-m", "ruff", "format", "--check", "src", "infrastructure", "tests", "scripts"],
            gate="H",
        ),
        Stage("types", "mypy type check", [py, "-m", "mypy"], gate="H"),
        Stage(
            "registry",
            "companies.json is up to date",
            [py, "scripts/build_company_registry.py", "--check"],
            gate="B",
        ),
        Stage("unit", "Level 1 - unit tests", [py, "-m", "pytest", "tests/unit", "-q"], gate="B"),
        Stage(
            "scrapers",
            "Level 2 - scraper/provider tests",
            [py, "-m", "pytest", "tests/scrapers", "-q", "-m", "not live"],
            gate="A",
        ),
        Stage(
            "integration",
            "Levels 3/4/6 - persistence, notifications, pipeline",
            [py, "-m", "pytest", "tests/integration", "-q"],
            gate="B",
        ),
        Stage(
            "e2e-local",
            "Level 8 - acceptance scenarios (in process)",
            [py, "-m", "pytest", "tests/e2e", "-q", "-m", "not aws_local"],
            gate="F",
        ),
        Stage(
            "mobile",
            "Level 5 - Expo app tests",
            ["npm", "run", "test:run", "--silent"],
            cwd=MOBILE,
            gate="C",
            needs_node=True,
        ),
        Stage(
            "mobile-types",
            "Expo app typechecks",
            ["npm", "run", "typecheck", "--silent"],
            cwd=MOBILE,
            gate="C",
            needs_node=True,
        ),
        Stage(
            "mobile-bundle",
            "Expo app bundles (Metro + Hermes)",
            [
                "npx",
                "expo",
                "export",
                "--platform",
                "ios",
                "--output-dir",
                str(REPO_ROOT / "build" / "expo"),
            ],
            cwd=MOBILE,
            gate="C",
            needs_node=True,
            after=("mobile-types",),
        ),
        Stage(
            "app",
            "Level 5 - web client component tests",
            ["npm", "run", "test:run", "--silent"],
            cwd=WEB,
            gate="C",
            needs_node=True,
        ),
        Stage(
            "app-build",
            "Web client typechecks and builds",
            ["npm", "run", "build", "--silent"],
            cwd=WEB,
            gate="C",
            needs_node=True,
        ),
        Stage(
            "app-e2e",
            "Level 5b - web client browser tests",
            ["npx", "playwright", "test"],
            cwd=WEB,
            gate="C",
            needs_node=True,
            after=("app-build",),
        ),
        Stage(
            "infra-synth",
            "Gate D - cdk synth",
            ["make", "infra-synth"],
            gate="D",
            needs_node=True,
        ),
        Stage(
            "infra-test",
            "Gate D - CDK template assertions",
            [py, "-m", "pytest", "infrastructure/tests", "-q"],
            gate="D",
            after=("infra-synth",),
        ),
        Stage(
            "infra-lint",
            "Gate D - cfn-lint",
            [str(REPO_ROOT / ".venv" / "bin" / "cfn-lint")]
            + [
                str(p)
                for p in sorted((REPO_ROOT / "infrastructure" / "cdk.out").glob("*.template.json"))
            ]
            or [str(REPO_ROOT / ".venv" / "bin" / "cfn-lint"), "--version"],
            gate="D",
            after=("infra-synth",),
        ),
        Stage(
            "localstack",
            "Gate E - boot LocalStack from scratch",
            ["make", "localstack-up"],
            gate="E",
            needs_docker=True,
        ),
        Stage(
            "provision",
            "Gate E - provision the architecture",
            ["make", "local-provision"],
            gate="E",
            needs_docker=True,
            after=("localstack",),
        ),
        Stage(
            "e2e-aws",
            "Level 7 - architecture on emulated AWS",
            ["make", "e2e-aws"],
            gate="E/F",
            needs_docker=True,
            after=("provision",),
        ),
    ]


def docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        return (
            subprocess.run(
                ["docker", "info"], capture_output=True, timeout=20, check=False
            ).returncode
            == 0
        )
    except (subprocess.SubprocessError, OSError):
        return False


def node_available() -> bool:
    return (
        shutil.which("npm") is not None
        and (MOBILE / "node_modules").is_dir()
        and (WEB / "node_modules").is_dir()
    )


def run_stage(stage: Stage, *, report_dir: Path) -> Result:
    started = time.monotonic()
    log_path = report_dir / f"{stage.key}.log"
    try:
        completed = subprocess.run(
            stage.command,
            cwd=stage.cwd,
            capture_output=True,
            text=True,
            timeout=stage.timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        log_path.write_text(f"timed out after {stage.timeout:.0f}s\n", encoding="utf-8")
        return Result(
            stage, "fail", time.monotonic() - started, f"timed out after {stage.timeout:.0f}s"
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log_path.write_text(str(exc), encoding="utf-8")
        return Result(stage, "fail", time.monotonic() - started, f"could not run: {exc}")

    output = (completed.stdout or "") + (completed.stderr or "")
    log_path.write_text(output, encoding="utf-8")
    lines = [line for line in output.splitlines() if line.strip()]
    elapsed = time.monotonic() - started

    if completed.returncode == 0:
        return Result(stage, "pass", elapsed, lines[-1][:120] if lines else "", lines[-8:])
    return Result(stage, "fail", elapsed, f"exit {completed.returncode}", lines[-25:])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-dir", type=Path, default=REPO_ROOT / ".verify")
    parser.add_argument("--no-docker", action="store_true", help="skip the LocalStack stages")
    parser.add_argument("--no-node", action="store_true", help="skip the client stages")
    parser.add_argument("--list", action="store_true", help="print the stages and exit")
    parser.add_argument("--keep-up", action="store_true", help="leave LocalStack running")
    args = parser.parse_args(argv)

    plan = stages()
    if args.list:
        for stage in plan:
            needs = " ".join(
                filter(
                    None,
                    ["docker" if stage.needs_docker else "", "node" if stage.needs_node else ""],
                )
            )
            print(
                f"  {stage.key:<14} gate {stage.gate:<5} {stage.title}{f'  [{needs}]' if needs else ''}"
            )
        return 0

    if not VENV_PY.exists():
        print(f"{VENV_PY} is missing - run `make setup` first", file=sys.stderr)
        return 2

    args.report_dir.mkdir(parents=True, exist_ok=True)
    have_docker = (not args.no_docker) and docker_available()
    have_node = (not args.no_node) and node_available()

    print(
        colour(
            f"\n{BOLD}make verify{RESET}" if not os.environ.get("NO_COLOR") else "make verify", BOLD
        )
    )
    print(f"  started   {datetime.now(UTC).isoformat(timespec='seconds')}")
    print(f"  docker    {'yes' if have_docker else 'no - LocalStack stages will SKIP'}")
    print(f"  node      {'yes' if have_node else 'no - client stages will SKIP'}")
    print(f"  logs      {args.report_dir.relative_to(REPO_ROOT)}/\n")

    results: list[Result] = []
    passed: set[str] = set()

    for stage in plan:
        label = f"  {stage.key:<14}"
        if stage.needs_docker and not have_docker:
            results.append(Result(stage, "skip", detail="docker unavailable"))
            print(f"{label}{colour('SKIP', GREY)}  docker unavailable")
            continue
        if stage.needs_node and not have_node:
            results.append(Result(stage, "skip", detail="node/node_modules unavailable"))
            print(f"{label}{colour('SKIP', GREY)}  node unavailable")
            continue
        missing = [key for key in stage.after if key not in passed]
        if missing:
            results.append(
                Result(stage, "skip", detail=f"prerequisite failed: {', '.join(missing)}")
            )
            print(f"{label}{colour('SKIP', GREY)}  prerequisite failed: {', '.join(missing)}")
            continue

        print(f"{label}{colour('....', GREY)}  {stage.title}", end="\r", flush=True)
        result = run_stage(stage, report_dir=args.report_dir)
        results.append(result)
        if result.status == "pass":
            passed.add(stage.key)
            print(
                f"{label}{colour('PASS', GREEN)}  {stage.title}  ({result.seconds:.1f}s)  {result.detail}"
            )
        else:
            print(
                f"{label}{colour('FAIL', RED)}  {stage.title}  ({result.seconds:.1f}s)  {result.detail}"
            )
            for line in result.tail:
                print(f"                  {colour(line[:150], GREY)}")

    if have_docker and not args.keep_up:
        subprocess.run(["make", "localstack-down"], cwd=REPO_ROOT, capture_output=True, check=False)
        print(f"  {'teardown':<14}{colour('DONE', GREY)}  LocalStack removed")

    failures = [r for r in results if r.status == "fail"]
    skipped = [r for r in results if r.status == "skip"]
    ran = [r for r in results if r.status != "skip"]

    print("\n" + "-" * 72)
    print(f"  {len(ran) - len(failures)}/{len(ran)} stage(s) passed", end="")
    if skipped:
        print(f", {len(skipped)} skipped", end="")
    print(f", total {sum(r.seconds for r in results):.0f}s")

    gates = sorted({r.stage.gate for r in results if r.stage.gate and r.status == "pass"})
    print(f"  gates with passing evidence: {', '.join(gates) or 'none'}")

    if skipped:
        print(colour("\n  PARTIAL RUN - these stages did not run:", YELLOW))
        for result in skipped:
            print(f"    {result.stage.key:<14} {result.detail}")

    verdict = (
        colour("  RESULT: FAIL", RED)
        if failures
        else colour("  RESULT: PASS", GREEN)
        + ("" if not skipped else colour("  (partial - see above)", YELLOW))
    )
    print("\n" + verdict + "\n")

    summary = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "result": "fail" if failures else "pass",
        "partial": bool(skipped),
        "stages": [
            {
                "key": r.stage.key,
                "title": r.stage.title,
                "gate": r.stage.gate,
                "status": r.status,
                "seconds": round(r.seconds, 2),
                "detail": r.detail,
            }
            for r in results
        ],
    }
    (args.report_dir / "summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
