"""
CDK Stack: Bedrock TPM Alarm Manager
- Lambda function to auto-manage CloudWatch Alarms and Dashboard for inference profiles
- EventBridge schedule for daily execution (0:00 UTC)
- Uses an existing SNS topic for alarm notifications
"""

from aws_cdk import (
    Stack,
    Duration,
    aws_lambda as lambda_,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_sns as sns,
)
from constructs import Construct


class BedrockTpmAlarmStack(Stack):
    def __init__(self, scope: Construct, id: str, *, sns_topic_arn: str, **kwargs):
        super().__init__(scope, id, **kwargs)

        topic = sns.Topic.from_topic_arn(self, "ExistingTopic", sns_topic_arn)

        fn = lambda_.Function(
            self, "BedrockTpmAlarmFn",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="bedrock_tpm_alarm_lambda.handler",
            code=lambda_.Code.from_asset("lambda"),
            timeout=Duration.minutes(5),
            environment={
                "SNS_TOPIC_ARN": sns_topic_arn,
                "THRESHOLD_PERCENT": "80",
                "REGION": self.region,
            },
        )

        fn.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "bedrock:ListInferenceProfiles",
                "servicequotas:ListServiceQuotas",
            ],
            resources=["*"],
        ))
        fn.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "cloudwatch:PutMetricAlarm",
                "cloudwatch:DescribeAlarms",
                "cloudwatch:DeleteAlarms",
                "cloudwatch:TagResource",
                "cloudwatch:PutDashboard",
            ],
            resources=["*"],
        ))
        topic.grant_publish(fn)

        # Daily at 0:00 UTC
        events.Rule(
            self, "DailySchedule",
            schedule=events.Schedule.cron(minute="0", hour="0"),
            targets=[targets.LambdaFunction(fn)],
        )
