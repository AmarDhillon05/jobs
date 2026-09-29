"""Automatic deploys: the GitHub OIDC deploy role and the workflow that uses it.

Neither can be exercised for real here (no GitHub runner, no AWS account), so
these pin the properties that make them safe: who may assume the role, what it
may do, and that the workflow only deploys tested code from master.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from aws_cdk import App, Environment
from aws_cdk.assertions import Template

from stacks.github_deploy_stack import GitHubDeployStack

pytestmark = pytest.mark.infra

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "deploy.yml"
ENV = Environment(account="123456789012", region="us-east-1")


def synth(**kwargs: Any) -> dict[str, Any]:
    stack = GitHubDeployStack(
        App(), "GitHubDeploy", repository="AmarDhillon05/jobs", env=ENV, **kwargs
    )
    return Template.from_stack(stack).to_json()


def of_type(template: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [r for r in template["Resources"].values() if r["Type"] == kind]


class TestDeployRole:
    @pytest.fixture(scope="class")
    @classmethod
    def template(cls) -> dict[str, Any]:
        return synth()

    def trust(self, template: dict[str, Any]) -> dict[str, Any]:
        [role] = of_type(template, "AWS::IAM::Role")
        [statement] = role["Properties"]["AssumeRolePolicyDocument"]["Statement"]
        return statement

    def test_creates_githubs_oidc_provider(self, template: dict[str, Any]) -> None:
        providers = of_type(template, "AWS::IAM::OIDCProvider")
        assert len(providers) == 1
        assert providers[0]["Properties"]["Url"] == "https://token.actions.githubusercontent.com"
        assert providers[0]["Properties"]["ClientIdList"] == ["sts.amazonaws.com"]

    def test_nothing_else_is_created(self, template: dict[str, Any]) -> None:
        kinds = sorted(r["Type"] for r in template["Resources"].values())
        assert kinds == ["AWS::IAM::OIDCProvider", "AWS::IAM::Policy", "AWS::IAM::Role"]

    def test_only_this_repo_and_branch_may_assume_it(self, template: dict[str, Any]) -> None:
        statement = self.trust(template)
        assert statement["Action"] == "sts:AssumeRoleWithWebIdentity"
        conditions = statement["Condition"]
        assert set(conditions) == {"StringEquals"}  # exact, no wildcards
        assert conditions["StringEquals"] == {
            "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
            "token.actions.githubusercontent.com:sub": (
                "repo:AmarDhillon05/jobs:ref:refs/heads/master"
            ),
        }

    def test_it_can_only_use_the_cdk_bootstrap_roles_and_read_cloudtrail(
        self, template: dict[str, Any]
    ) -> None:
        statements = [
            s
            for policy in of_type(template, "AWS::IAM::Policy")
            for s in policy["Properties"]["PolicyDocument"]["Statement"]
        ]
        actions = sorted(
            a
            for s in statements
            for a in ([s["Action"]] if isinstance(s["Action"], str) else s["Action"])
        )
        assert actions == ["cloudtrail:LookupEvents", "sts:AssumeRole", "sts:TagSession"]
        assume = next(s for s in statements if "sts:AssumeRole" in s["Action"])
        assert ":role/cdk-hnb659fds-*" in json.dumps(assume["Resource"])
        # No managed policies (AdministratorAccess or anything else) attached.
        [role] = of_type(template, "AWS::IAM::Role")
        assert "ManagedPolicyArns" not in role["Properties"]

    def test_sessions_are_short(self, template: dict[str, Any]) -> None:
        [role] = of_type(template, "AWS::IAM::Role")
        assert role["Properties"]["MaxSessionDuration"] == 3600

    def test_exports_the_role_arn_for_github(self, template: dict[str, Any]) -> None:
        assert "DeployRoleArn" in template["Outputs"]

    def test_another_branch_can_be_chosen(self) -> None:
        template = synth(branch="main")
        [role] = of_type(template, "AWS::IAM::Role")
        text = json.dumps(role["Properties"]["AssumeRolePolicyDocument"])
        assert "repo:AmarDhillon05/jobs:ref:refs/heads/main" in text

    def test_an_existing_provider_is_reused_not_duplicated(self) -> None:
        arn = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com"
        template = synth(oidc_provider_arn=arn)
        assert of_type(template, "AWS::IAM::OIDCProvider") == []
        [role] = of_type(template, "AWS::IAM::Role")
        assert arn in json.dumps(role["Properties"]["AssumeRolePolicyDocument"])

    def test_a_malformed_repository_is_refused(self) -> None:
        with pytest.raises(ValueError, match="owner/name"):
            GitHubDeployStack(App(), "Bad", repository="jobs", env=ENV)


class TestWorkflow:
    @pytest.fixture(scope="class")
    @classmethod
    def workflow(cls) -> dict[str, Any]:
        return yaml.safe_load(WORKFLOW.read_text())

    def test_deploy_waits_for_the_tests(self, workflow: dict[str, Any]) -> None:
        assert "test" in workflow["jobs"]["deploy"]["needs"]

    def test_deploy_only_from_a_push_to_master_once_configured(
        self, workflow: dict[str, Any]
    ) -> None:
        condition = workflow["jobs"]["deploy"]["if"]
        assert "github.event_name != 'pull_request'" in condition
        assert "github.ref == 'refs/heads/master'" in condition
        assert "vars.AWS_DEPLOY_ROLE_ARN != ''" in condition

    def test_only_the_deploy_job_may_request_an_oidc_token(self, workflow: dict[str, Any]) -> None:
        assert workflow["permissions"] == {"contents": "read"}
        for name, job in workflow["jobs"].items():
            granted = (job.get("permissions") or {}).get("id-token")
            assert granted == ("write" if name == "deploy" else None), name

    def test_deploys_never_overlap_or_get_cancelled_half_way(
        self, workflow: dict[str, Any]
    ) -> None:
        assert workflow["concurrency"]["cancel-in-progress"] is False

    def test_no_long_lived_aws_keys(self) -> None:
        text = WORKFLOW.read_text()
        assert "aws-access-key-id" not in text
        assert "AWS_SECRET_ACCESS_KEY" not in text

    def test_secrets_reach_the_deploy_only_through_env(self, workflow: dict[str, Any]) -> None:
        step = next(
            s
            for s in workflow["jobs"]["deploy"]["steps"]
            if s.get("name") == "Deploy JobMonitorStack"
        )
        assert "${{" not in step["run"]  # nothing interpolated into the shell
        assert set(step["env"]) == {"EMAIL_FROM", "EMAIL_TO", "NTFY_TOPIC"}
        assert "--require-approval never" in step["run"]

    def test_the_tests_run_the_same_gates_as_a_local_check(self, workflow: dict[str, Any]) -> None:
        runs = [s.get("run", "") for s in workflow["jobs"]["test"]["steps"]]
        for gate in ("make lint", "make typecheck", "make test", "make infra-validate"):
            assert gate in runs, gate

    def test_the_make_targets_it_uses_exist(self, workflow: dict[str, Any]) -> None:
        makefile = (REPO_ROOT / "Makefile").read_text()
        for target in ("setup-ci", "lint", "typecheck", "test", "infra-validate", "deploy"):
            assert f"\n{target}:" in makefile, target
        assert "$(CDK_DEPLOY_FLAGS)" in makefile
