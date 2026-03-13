#!/usr/bin/env python3
"""CDK application entrypoint for the Copilot Dispatch infrastructure stack.

Reads configuration from CDK context values defined in ``infra/cdk.json`` and
instantiates the ``CopilotDispatchStack`` with the resolved AWS region. The AWS
account is sourced from the ``CDK_DEFAULT_ACCOUNT`` environment variable (set
automatically by the CDK CLI when a profile is configured).

Usage::

    cd <repo-root>
    cdk synth          # synthesise the CloudFormation template
    cdk deploy         # deploy the stack to AWS
"""

from __future__ import annotations

import aws_cdk as cdk

from infra.stacks.copilot_dispatch_stack import CopilotDispatchStack

app = cdk.App()

stack_name: str = app.node.try_get_context("stack_name") or "copilot-dispatch"
region: str = app.node.try_get_context("aws_region") or "ap-southeast-2"

CopilotDispatchStack(
    app,
    stack_name,
    env=cdk.Environment(region=region),
)

app.synth()
