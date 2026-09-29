"""The role GitHub Actions assumes to deploy JobMonitorStack.

Deployed ONCE, by hand (``make deploy-github-role``); from then on every push
to the deploy branch redeploys the app through ``.github/workflows/deploy.yml``.

No AWS keys are stored in GitHub. The workflow exchanges a short-lived GitHub
OIDC token for this role's credentials, and the role trusts only:

* tokens issued by GitHub's OIDC provider, for the ``sts.amazonaws.com`` audience,
* from one repository, on one branch (``repo:<owner>/<repo>:ref:refs/heads/<branch>``).

It holds almost no permissions of its own: it may assume the roles ``cdk
bootstrap`` created (``cdk-hnb659fds-*``), which is how ``cdk deploy`` does
its work, and read CloudTrail so a failed deploy can print its real error
(``scripts/diagnose_deploy.py``).

An account can hold only one OIDC provider per URL. If yours already has
GitHub's (from another project), pass its ARN as ``-c githubOidcProviderArn=...``
and it is reused instead of created.
"""

from __future__ import annotations

from typing import Any

from aws_cdk import CfnOutput, Duration, Stack, Tags
from aws_cdk import aws_iam as iam
from constructs import Construct

GITHUB_OIDC_URL = "https://token.actions.githubusercontent.com"
GITHUB_OIDC_HOST = "token.actions.githubusercontent.com"
#: The qualifier `cdk bootstrap` uses unless told otherwise.
CDK_QUALIFIER = "hnb659fds"


class GitHubDeployStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        repository: str,
        branch: str = "master",
        oidc_provider_arn: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        Tags.of(self).add("project", "internship-job-monitor")
        if repository.count("/") != 1:
            raise ValueError(f"repository must be 'owner/name', got {repository!r}")

        if oidc_provider_arn:
            provider_arn = oidc_provider_arn
        else:
            # The native CloudFormation resource: no helper Lambda, unlike the
            # CDK's custom-resource provider. AWS no longer checks GitHub's
            # certificate thumbprint, so none is pinned.
            provider = iam.CfnOIDCProvider(
                self,
                "GitHubOidc",
                url=GITHUB_OIDC_URL,
                client_id_list=["sts.amazonaws.com"],
            )
            provider_arn = provider.attr_arn

        self.role = iam.Role(
            self,
            "DeployRole",
            description=f"GitHub Actions deploys JobMonitorStack from {repository}@{branch}",
            max_session_duration=Duration.hours(1),
            assumed_by=iam.WebIdentityPrincipal(
                provider_arn,
                conditions={
                    "StringEquals": {
                        f"{GITHUB_OIDC_HOST}:aud": "sts.amazonaws.com",
                        # Exact match: no other repository, branch, tag or pull request.
                        f"{GITHUB_OIDC_HOST}:sub": f"repo:{repository}:ref:refs/heads/{branch}",
                    },
                },
            ),
        )
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="UseTheCdkBootstrapRoles",
                actions=["sts:AssumeRole", "sts:TagSession"],
                resources=[f"arn:{self.partition}:iam::{self.account}:role/cdk-{CDK_QUALIFIER}-*"],
            )
        )
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="ExplainFailedDeploys",
                actions=["cloudtrail:LookupEvents"],
                resources=["*"],  # LookupEvents supports no resource-level scoping
            )
        )

        CfnOutput(
            self,
            "DeployRoleArn",
            value=self.role.role_arn,
            description="Set as the AWS_DEPLOY_ROLE_ARN repository variable on GitHub",
        )


__all__ = ["GitHubDeployStack"]
