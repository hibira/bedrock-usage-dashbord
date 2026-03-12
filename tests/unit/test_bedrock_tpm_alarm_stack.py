import aws_cdk as core
import aws_cdk.assertions as assertions

from bedrock_tpm_alarm.bedrock_tpm_alarm_stack import BedrockTpmAlarmStack

# example tests. To run these tests, uncomment this file along with the example
# resource in bedrock_tpm_alarm/bedrock_tpm_alarm_stack.py
def test_sqs_queue_created():
    app = core.App()
    stack = BedrockTpmAlarmStack(app, "bedrock-tpm-alarm")
    template = assertions.Template.from_stack(stack)

#     template.has_resource_properties("AWS::SQS::Queue", {
#         "VisibilityTimeout": 300
#     })
