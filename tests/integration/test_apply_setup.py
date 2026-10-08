"""`make apply-setup` (scripts/apply_setup.py) against Moto: never real AWS."""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import apply_setup  # noqa: E402

pytestmark = pytest.mark.integration


@pytest.fixture
def aws() -> Iterator[Any]:
    import boto3
    from moto import mock_aws

    with mock_aws():
        session = boto3.session.Session(region_name="us-east-1")
        session.client("s3").create_bucket(Bucket="apply-bucket")
        yield session


@pytest.fixture
def files(tmp_path: Path) -> tuple[Path, Path]:
    profile = tmp_path / "profile.json"
    profile.write_text(
        json.dumps({"first_name": "Alex", "last_name": "Rivera", "email": "a@x.edu"})
    )
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.7 resume")
    return profile, resume


def run(aws: Any, files: tuple[Path, Path], **overrides: Any) -> dict[str, str]:
    kwargs: dict[str, Any] = {
        "profile_path": files[0],
        "resume_path": files[1],
        "bucket": "apply-bucket",
        "stack": "JobMonitorStack",
        "anthropic_key": "sk-ant-test",
        "rotate_secret": False,
        "session": aws,
    }
    kwargs.update(overrides)
    return apply_setup.run(**kwargs)


def secret(aws: Any) -> str:
    return aws.client("ssm").get_parameter(
        Name=apply_setup.KIT_SECRET_PARAMETER, WithDecryption=True
    )["Parameter"]["Value"]


def test_uploads_and_stores_secrets(aws: Any, files: tuple[Path, Path]) -> None:
    result = run(aws, files)
    s3 = aws.client("s3")
    assert (
        s3.get_object(Bucket="apply-bucket", Key="resume.pdf")["Body"].read() == b"%PDF-1.7 resume"
    )
    assert (
        json.loads(s3.get_object(Bucket="apply-bucket", Key="profile.json")["Body"].read())[
            "first_name"
        ]
        == "Alex"
    )
    ssm = aws.client("ssm")
    key = ssm.get_parameter(Name=apply_setup.ANTHROPIC_KEY_PARAMETER, WithDecryption=True)[
        "Parameter"
    ]
    assert key["Value"] == "sk-ant-test" and key["Type"] == "SecureString"
    assert result["kit_secret"] == "created" and len(secret(aws)) >= 40


def test_the_secret_is_kept_unless_rotated(aws: Any, files: tuple[Path, Path]) -> None:
    run(aws, files)
    first = secret(aws)
    assert run(aws, files, anthropic_key=None)["kit_secret"] == "kept"
    assert secret(aws) == first
    assert run(aws, files, rotate_secret=True)["kit_secret"] == "rotated"
    assert secret(aws) != first


def test_bucket_from_the_stack_output(aws: Any, files: tuple[Path, Path]) -> None:
    class Outputs:
        def describe_stacks(self, StackName: str) -> dict[str, Any]:
            return {
                "Stacks": [
                    {"Outputs": [{"OutputKey": "ApplyBucketName", "OutputValue": "apply-bucket"}]}
                ]
            }

    class Session:
        def client(self, name: str) -> Any:
            return Outputs() if name == "cloudformation" else aws.client(name)

    assert run(aws, files, bucket=None, session=Session())["bucket"] == "apply-bucket"


@pytest.mark.parametrize(
    ("profile", "resume", "message"),
    [
        ("{nope", b"%PDF", "valid JSON"),
        ('{"first_name": "A"}', b"%PDF", "email"),
        ('{"first_name": "A", "last_name": "B", "email": "e"}', b"hello", "not a PDF"),
    ],
)
def test_bad_inputs(tmp_path: Path, profile: str, resume: bytes, message: str) -> None:
    p, r = tmp_path / "p.json", tmp_path / "r.pdf"
    p.write_text(profile)
    r.write_bytes(resume)
    with pytest.raises(SystemExit, match=message):
        apply_setup.check_inputs(p, r)
