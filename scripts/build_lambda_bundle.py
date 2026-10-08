#!/usr/bin/env python3
"""Assemble the Lambda deployment bundle.

The bundle is deliberately trivial: the ``jobmonitor`` package plus
``companies.json``. There is nothing to compile and nothing to pip-install,
because the backend depends only on the standard library plus boto3/botocore/
urllib3 - all of which the Lambda Python runtime already provides. That is what
lets ``cdk synth`` run with no Docker and no network.

    python scripts/build_lambda_bundle.py            # -> build/lambda/
    python scripts/build_lambda_bundle.py --clean
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_PACKAGE = REPO_ROOT / "src" / "jobmonitor"
REGISTRY = REPO_ROOT / "companies.json"
BUNDLE_DIR = REPO_ROOT / "build" / "lambda"

#: Never ship caches or test leftovers into a deployment artifact.
EXCLUDE = shutil.ignore_patterns(
    "__pycache__", "*.pyc", "*.pyo", ".pytest_cache", ".mypy_cache", "*.egg-info"
)


def build(*, clean: bool = True, bundle_dir: Path = BUNDLE_DIR) -> Path:
    if not SOURCE_PACKAGE.is_dir():
        raise SystemExit(f"missing source package: {SOURCE_PACKAGE}")
    if not REGISTRY.is_file():
        raise SystemExit(f"missing registry: {REGISTRY} (run `make companies`)")

    if clean and bundle_dir.exists():
        shutil.rmtree(bundle_dir)
    bundle_dir.mkdir(parents=True, exist_ok=True)

    target_package = bundle_dir / "jobmonitor"
    if target_package.exists():
        shutil.rmtree(target_package)
    shutil.copytree(SOURCE_PACKAGE, target_package, ignore=EXCLUDE)
    shutil.copy2(REGISTRY, bundle_dir / "companies.json")
    return bundle_dir


#: The Kit Lambda's layer: the Anthropic SDK for the Apply kit's drafter, and
#: nothing else. Only the Kit Lambda gets it; every other function keeps the
#: dependency-free bundle above.
LAYER_DIR = REPO_ROOT / "build" / "layer-anthropic"
LAYER_REQUIREMENT = "anthropic>=1.12,<2"
#: Must match the functions: Python 3.12 on ARM (job_monitor_stack.RUNTIME / ARM_64).
LAYER_PLATFORM = "manylinux2014_aarch64"
LAYER_PYTHON = "3.12"


def build_layer(*, layer_dir: Path = LAYER_DIR) -> Path:
    """``python/`` with the Anthropic SDK, from Linux/ARM wheels; no Docker needed.

    pip resolves wheels for the Lambda platform rather than this machine's, so the
    layer is right however it is built. A stamp records what was installed, so
    repeat syntheses (and the template tests) reuse it instead of re-downloading.
    """
    stamp = layer_dir / ".stamp"
    wanted = f"{LAYER_REQUIREMENT} {LAYER_PLATFORM} cp{LAYER_PYTHON}"
    if stamp.exists() and stamp.read_text() == wanted:
        return layer_dir
    if layer_dir.exists():
        shutil.rmtree(layer_dir)
    target = layer_dir / "python"
    target.mkdir(parents=True)
    import subprocess

    command = [
        sys.executable, "-m", "pip", "install", "--quiet", "--no-compile",
        "--target", str(target),
        "--platform", LAYER_PLATFORM,
        "--implementation", "cp",
        "--python-version", LAYER_PYTHON,
        "--only-binary=:all:",
        LAYER_REQUIREMENT,
    ]  # fmt: skip
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        shutil.rmtree(layer_dir, ignore_errors=True)
        raise SystemExit(
            "could not build the Kit Lambda's anthropic layer (pip needs network access):\n"
            + result.stderr[-2000:]
        )
    for cache in target.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)
    stamp.write_text(wanted)
    return layer_dir


def bundle_summary(bundle_dir: Path = BUNDLE_DIR) -> dict[str, object]:
    files = [path for path in bundle_dir.rglob("*") if path.is_file()]
    return {
        "path": str(bundle_dir),
        "files": len(files),
        "bytes": sum(path.stat().st_size for path in files),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clean", action="store_true", help="remove the bundle first")
    parser.add_argument("--out", type=Path, default=BUNDLE_DIR)
    parser.add_argument("--layer", action="store_true", help="also build the anthropic layer")
    args = parser.parse_args(argv)
    if args.layer:
        layer = build_layer()
        print(f"layer -> {layer.relative_to(REPO_ROOT)} ({bundle_summary(layer)['files']} files)")

    bundle = build(clean=args.clean or True, bundle_dir=args.out)
    summary = bundle_summary(bundle)
    print(
        f"bundled {summary['files']} files "
        f"({int(summary['bytes']) // 1024} KiB) -> {bundle.relative_to(REPO_ROOT)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
