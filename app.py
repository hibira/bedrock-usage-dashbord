#!/usr/bin/env python3
import os

import aws_cdk as cdk
from bedrock_tpm_alarm.bedrock_tpm_alarm_stack import BedrockTpmAlarmStack

app = cdk.App()
model_filter = app.node.try_get_context("model_filter") or ""

# Multi-region: sns_topic_arns="us-east-1=arn:...,us-west-2=arn:..."
# Single-region (backward compat): sns_topic_arn=arn:...
sns_topic_arns_str = app.node.try_get_context("sns_topic_arns") or ""
sns_topic_arn = app.node.try_get_context("sns_topic_arn") or os.environ.get("SNS_TOPIC_ARN")

if sns_topic_arns_str:
    account = os.environ.get("CDK_DEFAULT_ACCOUNT")
    for pair in sns_topic_arns_str.split(","):
        region, arn = pair.split("=", 1)
        region, arn = region.strip(), arn.strip()
        BedrockTpmAlarmStack(
            app,
            f"BedrockQuotaAlarmStack-{region}",
            sns_topic_arn=arn,
            model_filter=model_filter,
            env=cdk.Environment(account=account, region=region),
        )
elif sns_topic_arn:
    BedrockTpmAlarmStack(
        app,
        "BedrockTpmAlarmStack",
        sns_topic_arn=sns_topic_arn,
        model_filter=model_filter,
    )
else:
    raise ValueError(
        "Provide sns_topic_arns (multi-region) or sns_topic_arn (single-region) via -c context"
    )

app.synth()
