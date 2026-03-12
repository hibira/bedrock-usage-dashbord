# Bedrock Quota Alarm Manager

Automatically creates and updates CloudWatch Alarms and a Dashboard for Bedrock inference profile **TPM** (`EstimatedTPMQuotaUsage`) and **RPM** (`Invocations` Sum) metrics.

`EstimatedTPMQuotaUsage` represents the estimated quota consumption per minute (not a percentage), calculated as:

```
InputTokenCount + CacheWriteInputTokens + (OutputTokenCount × burndown rate)
```

The burndown rate is 5× for Claude 3.7+ models (1× for others). Alarm thresholds are derived from actual Service Quotas (TPM/RPM limits per model).

## Features

- Fetches both SYSTEM_DEFINED and APPLICATION inference profiles
- Groups profiles by (model, quota type) — Global and Regional quotas are monitored separately
- Creates CloudWatch Alarms for both TPM and RPM with thresholds based on actual Service Quotas
- Builds a CloudWatch Dashboard showing TPM and RPM usage as % of quota via metric math
- Cleans up stale alarms for removed profiles/models
- Supports filtering to monitor only specific models
- Runs daily at 0:00 UTC (9:00 JST) via EventBridge

## Quota Grouping

| Quota Type | Profiles | Quota Source |
|---|---|---|
| Regional | `us.*`, `eu.*`, `ap.*` + application profiles (regional model ARN) | `Cross-region model inference tokens/requests per minute` |
| Global | `global.*` + application profiles (regionless model ARN) | `Global cross-region model inference tokens/requests per minute` |

> **Note:** 1M Context Length quotas exist separately in Service Quotas but cannot be monitored per-profile, as the same profile serves both standard and 1M requests depending on input size.

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
| `DASHBOARD_NAME` | CloudWatch Dashboard name | `Bedrock-Quota-Usage` |
| `MODEL_FILTER` | Comma-separated model name patterns (partial match). Empty = all models | `""` |

## CDK Context Parameters

| Parameter | Description |
|-----------|-------------|
| `sns_topic_arn` | SNS topic ARN for alarm notifications (required) |
| `model_filter` | Comma-separated model name patterns (optional) |

## How It Works

1. Fetches all inference profiles (system-defined + application)
2. Groups profiles by (model name, quota type: global/regional)
3. Fetches TPM and RPM quotas from Service Quotas API
4. For each group, creates CloudWatch Alarms (TPM + RPM) where:
   - Threshold = `quota × THRESHOLD_PERCENT / 100`
   - Multiple profiles for the same group are summed via metric math
5. Builds a dashboard with per-group TPM and RPM graphs showing usage as % of quota
6. Deletes stale alarms for groups no longer in scope
