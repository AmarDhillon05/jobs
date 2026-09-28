#!/usr/bin/env python3
"""CDK entry point.

No account or region is baked in: the stack is environment-agnostic so `cdk synth`
runs offline with no credentials, which is what makes infrastructure validation
part of the ordinary test suite.

    cd infrastructure && cdk synth          # validate
    cd infrastructure && cdk deploy         # deliberate, by the user, later
"""

from __future__ import annotations

import os

import aws_cdk as cdk

from stacks.job_monitor_stack import JobMonitorStack

app = cdk.App()

JobMonitorStack(
    app,
    "JobMonitorStack",
    description="Monitors ~90 companies for newly posted technical internships",
    # Values a deployer will want to change, surfaced as CDK context rather than
    # buried in the stack.
    shard_size=int(app.node.try_get_context("shardSize") or 8),
    notify_threshold=int(app.node.try_get_context("notifyThreshold") or 55),
    keep_threshold=int(app.node.try_get_context("keepThreshold") or 35),
    email_from=str(app.node.try_get_context("emailFrom") or "alerts@example.com"),
    email_to=str(app.node.try_get_context("emailTo") or "you@example.com"),
    app_base_url=str(app.node.try_get_context("appBaseUrl") or "https://app.example.com"),
    # Your private ntfy topic (e.g. `-c ntfyTopic=...`). Leave unset for SNS push.
    ntfy_topic=app.node.try_get_context("ntfyTopic") or None,
    ntfy_server=str(app.node.try_get_context("ntfyServer") or "https://ntfy.sh"),
    env=cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=os.environ.get("CDK_DEFAULT_REGION", "us-east-1"),
    ),
)

app.synth()
