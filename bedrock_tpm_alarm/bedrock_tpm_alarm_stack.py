"""
CDK Stack: Bedrock TPM Alarm Manager
- Lambda 関数（日次で推論プロファイルの CloudWatch Alarm を自動管理）
- EventBridge スケジュール（毎日 AM 9:00 JST = 0:00 UTC）
- 既存 SNS トピックを利用
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
            actions=["bedrock:ListInferenceProfiles"],
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

        # 毎日 0:00 UTC (= 9:00 JST)
        events.Rule(
            self, "DailySchedule",
            schedule=events.Schedule.cron(minute="0", hour="0"),
            targets=[targets.LambdaFunction(fn)],
        )
