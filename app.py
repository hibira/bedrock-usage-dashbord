#!/usr/bin/env python3
import os

import aws_cdk as cdk
from bedrock_tpm_alarm.bedrock_tpm_alarm_stack import BedrockTpmAlarmStack

app = cdk.App()

sns_topic_arn = app.node.try_get_context("sns_topic_arn") or os.environ.get("SNS_TOPIC_ARN")
if not sns_topic_arn:
    raise ValueError("sns_topic_arn must be set via context (-c sns_topic_arn=...) or SNS_TOPIC_ARN env var")

model_filter = app.node.try_get_context("model_filter") or ""

BedrockTpmAlarmStack(
    app,
    "BedrockTpmAlarmStack",
    sns_topic_arn=sns_topic_arn,
    model_filter=model_filter,
)
app.synth()
