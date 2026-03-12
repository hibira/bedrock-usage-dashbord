# Bedrock TPM Alarm Manager

Automatically creates and updates CloudWatch Alarms and a Dashboard for Bedrock inference profile `EstimatedTPMQuotaUsage` metrics.

## Features

- Fetches all inference profiles via `list-inference-profiles`
- Creates/updates a CloudWatch Alarm for each ACTIVE profile
- Builds a CloudWatch Dashboard grouped by underlying model name
- Cleans up stale alarms for deleted profiles
- Runs daily at 0:00 UTC (9:00 JST) via EventBridge

## Deploy

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

npx cdk deploy -c sns_topic_arn=arn:aws:sns:us-east-1:123456789012:your-topic
```

## Lambda Environment Variables

| Name | Description | Default |
|------|-------------|---------|
| `SNS_TOPIC_ARN` | SNS topic ARN for alarm notifications | (required) |
| `THRESHOLD_PERCENT` | Alarm threshold (%) | `80` |
| `REGION` | AWS region | `us-east-1` |
| `DASHBOARD_NAME` | CloudWatch Dashboard name | `Bedrock-TPM-Usage` |
