#!/usr/bin/env python3
"""Set up the Apply kit in YOUR deployed stack (run it yourself, after `make deploy`).

    make apply-setup PROFILE=apply/profile.json RESUME=~/resume.pdf ANTHROPIC_API_KEY=sk-ant-...

It uses your own AWS credentials, like `make deploy`, and:

1. checks that the profile is valid JSON and the resume is a PDF;
2. uploads both to the stack's private ``ApplyBucket`` (found from the stack's
   ``ApplyBucketName`` output, or ``--bucket``);
3. stores the Anthropic API key as an SSM SecureString (``/jobmonitor/anthropic-api-key``)
   when ``ANTHROPIC_API_KEY`` is set;
4. creates the kit's link-signing secret (``/jobmonitor/kit-secret``) if it does not
   exist yet. ``--rotate-secret`` replaces it, which invalidates every kit link sent
   so far.

Run it again whenever your resume or profile changes; the kit reads them fresh on
its next cold start (within minutes).
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from jobmonitor.apply.profile import Profile, ProfileError  # noqa: E402

KIT_SECRET_PARAMETER = "/jobmonitor/kit-secret"
ANTHROPIC_KEY_PARAMETER = "/jobmonitor/anthropic-api-key"
MAX_RESUME_BYTES = 10 * 1024 * 1024


class SetupError(SystemExit):
    pass


def check_inputs(profile_path: Path, resume_path: Path) -> tuple[bytes, bytes]:
    try:
        profile_bytes = profile_path.expanduser().read_bytes()
    except OSError as exc:
        raise SetupError(f"cannot read profile {profile_path}: {exc}") from exc
    try:
        profile = Profile.from_json(profile_bytes.decode("utf-8"))
    except (ProfileError, UnicodeDecodeError) as exc:
        raise SetupError(f"{profile_path}: {exc}") from exc
    if not profile.value("email") or not profile.full_name:
        raise SetupError(f"{profile_path}: fill in at least first_name, last_name and email")
    try:
        resume = resume_path.expanduser().read_bytes()
    except OSError as exc:
        raise SetupError(f"cannot read resume {resume_path}: {exc}") from exc
    if not resume.startswith(b"%PDF"):
        raise SetupError(f"{resume_path} is not a PDF")
    if len(resume) > MAX_RESUME_BYTES:
        raise SetupError(f"{resume_path} is over 10 MB")
    return profile_bytes, resume


def stack_output(cloudformation: Any, stack: str, key: str) -> str:
    try:
        described = cloudformation.describe_stacks(StackName=stack)["Stacks"][0]
    except Exception as exc:
        raise SetupError(f"cannot read stack {stack} (deployed? right region?): {exc}") from exc
    for output in described.get("Outputs", []):
        if output.get("OutputKey") == key:
            return str(output["OutputValue"])
    raise SetupError(f"stack {stack} has no {key} output: redeploy with this version first")


def ensure_secret(ssm: Any, *, rotate: bool) -> str:
    """The link-signing secret: created once, kept, replaced only on request."""
    if not rotate:
        try:
            ssm.get_parameter(Name=KIT_SECRET_PARAMETER, WithDecryption=True)
            return "kept"
        except ssm.exceptions.ParameterNotFound:
            pass
    ssm.put_parameter(
        Name=KIT_SECRET_PARAMETER,
        Value=secrets.token_urlsafe(32),
        Type="SecureString",
        Overwrite=True,
        Description="Signs Apply-kit links (internship monitor)",
    )
    return "rotated" if rotate else "created"


def run(
    *,
    profile_path: Path,
    resume_path: Path,
    bucket: str | None,
    stack: str,
    anthropic_key: str | None,
    rotate_secret: bool,
    session: Any,
) -> dict[str, str]:
    profile_bytes, resume = check_inputs(profile_path, resume_path)
    bucket = bucket or stack_output(session.client("cloudformation"), stack, "ApplyBucketName")
    s3 = session.client("s3")
    s3.put_object(
        Bucket=bucket, Key="profile.json", Body=profile_bytes, ContentType="application/json"
    )
    s3.put_object(Bucket=bucket, Key="resume.pdf", Body=resume, ContentType="application/pdf")
    ssm = session.client("ssm")
    result = {"bucket": bucket, "kit_secret": ensure_secret(ssm, rotate=rotate_secret)}
    if anthropic_key:
        ssm.put_parameter(
            Name=ANTHROPIC_KEY_PARAMETER,
            Value=anthropic_key,
            Type="SecureString",
            Overwrite=True,
            Description="Anthropic API key for Apply-kit drafts (internship monitor)",
        )
        result["anthropic_key"] = "stored"
    else:
        result["anthropic_key"] = "unchanged (set ANTHROPIC_API_KEY to store one)"
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--profile", type=Path, default=REPO_ROOT / "apply" / "profile.json")
    parser.add_argument("--resume", type=Path, required=True)
    parser.add_argument("--bucket", help="the ApplyBucket name (default: from the stack outputs)")
    parser.add_argument("--stack", default="JobMonitorStack")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION") or "us-east-1")
    parser.add_argument("--rotate-secret", action="store_true", help="invalidate all kit links")
    args = parser.parse_args(argv)

    import boto3

    session = boto3.session.Session(region_name=args.region)
    result = run(
        profile_path=args.profile,
        resume_path=args.resume,
        bucket=args.bucket,
        stack=args.stack,
        anthropic_key=os.environ.get("ANTHROPIC_API_KEY") or None,
        rotate_secret=args.rotate_secret,
        session=session,
    )
    print(f"profile.json and resume.pdf -> s3://{result['bucket']}")
    print(f"kit link secret: {result['kit_secret']}")
    print(f"Anthropic key: {result['anthropic_key']}")
    print("Done. The next internship alert's 'Apply kit' button opens your kit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
