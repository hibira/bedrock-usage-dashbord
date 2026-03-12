# Bedrock TPM Alarm Manager

Automatically creates and updates CloudWatch Alarms and a Dashboard for Bedrock inference profile `EstimatedTPMQuotaUsage` metrics.

Alarm thresholds are derived from actual Service Quotas (TPM limits per model), and the dashboard displays usage as a percentage of quota.

## Features

- Fetches both SYSTEM_DEFINED and APPLICATION inference profiles
- Groups profiles by underlying model and sums TPM usage per model
- Creates CloudWatch Alarms with thresholds based on actual Service Quotas
- Builds a CloudWatch Dashboard showing usage as % of quota via metric math
- Cleans up stale alarms for removed profiles/models
- Supports filtering to monitor only specific models
- Runs daily at 0:00 UTC (9:00 JST) via EventBridge

## Deploy

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Monitor specific models only
npx cdk deploy \
  -c sns_topic_arn=arn:aws:sns:us-east-1:123456789012:your-topic \
  -c model_filter="claude-sonnet-4-6,claude-sonnet-4-5,claude-opus-4-5,claude-opus-4-6"

# Monitor all models (omit model_filter)
npx cdk deploy -c sns_topic_arn=arn:aws:sns:us-east-1:123456789012:your-topic
```

## Lambda Environment Variables

| Name | Description | Default |
|------|-------------|---------|
| `SNS_TOPIC_ARN` | SNS topic ARN for alarm notifications | (required) |
| `THRESHOLD_PERCENT` | Alarm threshold as % of quota | `80` |
| `REGION` | AWS region | `us-east-1` |
| `DASHBOARD_NAME` | CloudWatch Dashboard name | `Bedrock-TPM-Usage` |
| `MODEL_FILTER` | Comma-separated model name patterns (partial match). Empty = all models | `""` |

## CDK Context Parameters

| Parameter | Description |
|-----------|-------------|
| `sns_topic_arn` | SNS topic ARN for alarm notifications (required) |
| `model_filter` | Comma-separated model name patterns (optional) |

## How It Works

1. Fetches all inference profiles (system-defined + application)
2. Groups profiles by underlying model name
3. Fetches TPM quotas from Service Quotas API
4. For each model, creates a CloudWatch Alarm where:
   - Threshold = `quota_tpm × THRESHOLD_PERCENT / 100`
   - Multiple profiles for the same model are summed via metric math
5. Builds a dashboard with per-model graphs showing usage as % of quota
6. Deletes alarms for models no longer in scope
