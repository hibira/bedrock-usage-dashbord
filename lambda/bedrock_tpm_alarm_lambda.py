"""
Auto-create/update CloudWatch Alarms and Dashboard for Bedrock inference profile
EstimatedTPMQuotaUsage metrics. Alarms are created per model (sum of all inference
profiles for the same model) with thresholds derived from actual Service Quotas.
Dashboard shows usage as a percentage of quota. Runs daily via EventBridge.
"""

import json
import os
import re
from collections import defaultdict

import boto3

REGION = os.environ.get("REGION", "us-east-1")
SNS_TOPIC_ARN = os.environ["SNS_TOPIC_ARN"]
THRESHOLD_PERCENT = float(os.environ.get("THRESHOLD_PERCENT", "80"))
ALARM_PREFIX = "Bedrock-TPM-"
DASHBOARD_NAME = os.environ.get("DASHBOARD_NAME", "Bedrock-TPM-Usage")

bedrock = boto3.client("bedrock", region_name=REGION)
cw = boto3.client("cloudwatch", region_name=REGION)
sq = boto3.client("service-quotas", region_name=REGION)


def get_inference_profiles():
    """Fetch all inference profiles and return a mapping of profile ID to info."""
    profiles = {}
    paginator = bedrock.get_paginator("list_inference_profiles")
    for page in paginator.paginate():
        for p in page["inferenceProfileSummaries"]:
            pid = p["inferenceProfileId"]
            model_arn = p.get("models", [{}])[0].get("modelArn", "unknown")
            model_name = model_arn.rsplit("/", 1)[-1] if "/" in model_arn else model_arn
            # Determine profile type from ID prefix
            if pid.startswith("global."):
                profile_type = "global"
            elif pid.startswith("us.") or pid.startswith("eu.") or pid.startswith("ap."):
                profile_type = "cross-region"
            else:
                profile_type = "on-demand"
            profiles[pid] = {
                "model_name": model_name,
                "profile_name": p.get("inferenceProfileName", pid),
                "status": p.get("status"),
                "profile_type": profile_type,
            }
    return profiles


def get_tpm_quotas():
    """Fetch all Bedrock TPM quotas from Service Quotas.
    Returns dict: {quota_name_lower: {"code": str, "value": float, "name": str}}
    """
    quotas = {}
    paginator = sq.get_paginator("list_service_quotas")
    for page in paginator.paginate(ServiceCode="bedrock"):
        for q in page["Quotas"]:
            name = q["QuotaName"]
            if "tokens per minute" in name.lower():
                quotas[name.lower()] = {
                    "code": q["QuotaCode"],
                    "value": q["Value"],
                    "name": name,
                }
    return quotas


def _normalize(s):
    """Normalize a string for fuzzy matching."""
    s = s.lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def match_quota(model_name, profile_type, quotas):
    """Find the best matching TPM quota for a model + profile type.
    Returns quota value (float) or None if not found.
    """
    # Build search keywords from model name
    # e.g. "anthropic.claude-sonnet-4-6" -> "anthropic claude sonnet 4 6"
    model_keywords = _normalize(model_name.replace(".", " ").replace("-", " "))

    # Determine quota name prefix based on profile type
    if profile_type == "global":
        prefix = "global cross-region"
    elif profile_type == "cross-region":
        prefix = "cross-region"
    else:
        prefix = "on-demand"

    best_match = None
    best_score = 0

    for qname_lower, qinfo in quotas.items():
        if "tokens per minute" not in qname_lower:
            continue
        # Check prefix match
        if prefix not in qname_lower:
            continue
        # Skip 1M context length variants (separate quota)
        if "1m context" in qname_lower:
            continue

        # Score by counting matching keywords
        q_normalized = _normalize(qinfo["name"])
        model_words = model_keywords.split()
        score = sum(1 for w in model_words if w in q_normalized)

        if score > best_score:
            best_score = score
            best_match = qinfo

    return best_match["value"] if best_match and best_score >= 2 else None


def group_by_model(profiles):
    """Group active profiles by their underlying model name."""
    by_model = defaultdict(list)
    for pid, info in profiles.items():
        if info.get("status") == "ACTIVE":
            by_model[info["model_name"]].append((pid, info))
    return by_model


def put_model_alarm(model_name, profile_entries, quota_value):
    """Create/update a CloudWatch Alarm using metric math.
    Threshold is set to quota_value * THRESHOLD_PERCENT / 100."""
    safe_name = model_name.replace(".", "-").replace(":", "-")
    alarm_name = f"{ALARM_PREFIX}{safe_name}"

    threshold = quota_value * THRESHOLD_PERCENT / 100

    metrics = []
    metric_ids = []
    for i, (pid, info) in enumerate(profile_entries):
        mid = f"m{i}"
        metric_ids.append(mid)
        metrics.append({
            "Id": mid,
            "MetricStat": {
                "Metric": {
                    "Namespace": "AWS/Bedrock",
                    "MetricName": "EstimatedTPMQuotaUsage",
                    "Dimensions": [{"Name": "ModelId", "Value": pid}],
                },
                "Period": 60,
                "Stat": "Maximum",
            },
            "ReturnData": False,
        })

    if len(metric_ids) == 1:
        metrics[0]["ReturnData"] = True
        metrics[0]["Id"] = "total"
    else:
        metrics.append({
            "Id": "total",
            "Expression": "+".join(metric_ids),
            "Label": f"{model_name} Total TPM",
            "ReturnData": True,
        })

    profile_names = ", ".join(info["profile_name"] for _, info in profile_entries)
    cw.put_metric_alarm(
        AlarmName=alarm_name,
        AlarmDescription=(
            f"Bedrock TPM monitor: {model_name} | "
            f"quota: {quota_value:,.0f} TPM, threshold: {threshold:,.0f} TPM ({THRESHOLD_PERCENT}%) | "
            f"profiles: {profile_names}"
        ),
        Metrics=metrics,
        EvaluationPeriods=1,
        Threshold=threshold,
        ComparisonOperator="GreaterThanOrEqualToThreshold",
        TreatMissingData="notBreaching",
        AlarmActions=[SNS_TOPIC_ARN],
        OKActions=[SNS_TOPIC_ARN],
        Tags=[
            {"Key": "ManagedBy", "Value": "bedrock-tpm-alarm-lambda"},
            {"Key": "ModelName", "Value": model_name},
            {"Key": "QuotaTPM", "Value": str(int(quota_value))},
        ],
    )
    return alarm_name


def cleanup_stale_alarms(active_alarm_names):
    """Delete alarms that are no longer needed."""
    paginator = cw.get_paginator("describe_alarms")
    stale = []
    for page in paginator.paginate(AlarmNamePrefix=ALARM_PREFIX):
        for a in page["MetricAlarms"]:
            if a["AlarmName"] not in active_alarm_names:
                stale.append(a["AlarmName"])
    if stale:
        cw.delete_alarms(AlarmNames=stale)
    return stale


def build_dashboard(by_model, model_quotas):
    """Create/update a CloudWatch Dashboard showing usage as % of quota."""
    widgets = []
    y = 0

    widgets.append({
        "type": "text",
        "x": 0, "y": y, "width": 24, "height": 1,
        "properties": {
            "markdown": f"# Bedrock TPM Quota Usage (alarm threshold: {THRESHOLD_PERCENT}%)"
        },
    })
    y += 1

    for model_name, profiles in sorted(by_model.items()):
        quota = model_quotas.get(model_name)
        quota_label = f" (quota: {quota:,.0f} TPM)" if quota else " (quota: unknown)"

        widgets.append({
            "type": "text",
            "x": 0, "y": y, "width": 24, "height": 1,
            "properties": {"markdown": f"## {model_name}{quota_label}"},
        })
        y += 1

        if quota:
            # Show as percentage of quota using metric math
            metrics_def = []
            metric_ids = []
            for i, (pid, info) in enumerate(profiles):
                mid = f"m{i}"
                metric_ids.append(mid)
                metrics_def.append({
                    "expression": "",
                    "id": mid,
                })

            raw_metrics = []
            expr_parts = []
            for i, (pid, info) in enumerate(profiles):
                mid = f"raw{i}"
                pct_id = f"pct{i}"
                expr_parts.append(mid)
                raw_metrics.append([
                    "AWS/Bedrock", "EstimatedTPMQuotaUsage",
                    "ModelId", pid,
                    {"id": mid, "visible": False, "stat": "Maximum"},
                ])
                raw_metrics.append([{
                    "expression": f"{mid}/{quota}*100",
                    "label": f"{info['profile_name']} (%)",
                    "id": pct_id,
                }])

            # Total percentage
            total_expr = f"({'+'.join(expr_parts)})/{quota}*100"
            raw_metrics.append([{
                "expression": total_expr,
                "label": "Total (%)",
                "id": "total_pct",
                "color": "#d62728",
            }])

            widgets.append({
                "type": "metric",
                "x": 0, "y": y, "width": 24, "height": 6,
                "properties": {
                    "metrics": raw_metrics,
                    "view": "timeSeries",
                    "region": REGION,
                    "title": f"{model_name} - TPM Quota Usage (%)",
                    "period": 60,
                    "yAxis": {"left": {"min": 0, "max": 100, "label": "%"}},
                    "annotations": {
                        "horizontal": [
                            {"label": f"Threshold ({THRESHOLD_PERCENT}%)",
                             "value": THRESHOLD_PERCENT, "color": "#d62728"},
                        ]
                    },
                },
            })
        else:
            # No quota found - show raw TPM values
            raw_metrics = []
            for pid, info in profiles:
                raw_metrics.append([
                    "AWS/Bedrock", "EstimatedTPMQuotaUsage",
                    "ModelId", pid,
                    {"label": info["profile_name"], "stat": "Maximum"},
                ])
            widgets.append({
                "type": "metric",
                "x": 0, "y": y, "width": 24, "height": 6,
                "properties": {
                    "metrics": raw_metrics,
                    "view": "timeSeries",
                    "region": REGION,
                    "title": f"{model_name} - TPM (raw, quota unknown)",
                    "period": 60,
                },
            })
        y += 6

    cw.put_dashboard(
        DashboardName=DASHBOARD_NAME,
        DashboardBody=json.dumps({"widgets": widgets}),
    )


def handler(event, context):
    profiles = get_inference_profiles()
    print(f"Found {len(profiles)} inference profiles")

    quotas = get_tpm_quotas()
    print(f"Found {len(quotas)} TPM quotas")

    by_model = group_by_model(profiles)
    print(f"Grouped into {len(by_model)} models")

    # Resolve quota for each model
    model_quotas = {}
    for model_name, entries in by_model.items():
        profile_type = entries[0][1]["profile_type"]
        quota_value = match_quota(model_name, profile_type, quotas)
        if quota_value:
            model_quotas[model_name] = quota_value

    print(f"Matched quotas for {len(model_quotas)}/{len(by_model)} models")

    created = []
    skipped = []
    for model_name, entries in by_model.items():
        quota = model_quotas.get(model_name)
        if quota:
            name = put_model_alarm(model_name, entries, quota)
            created.append(name)
            print(f"Upserted alarm: {name} (quota: {quota:,.0f}, threshold: {quota * THRESHOLD_PERCENT / 100:,.0f})")
        else:
            skipped.append(model_name)
            print(f"Skipped (no quota found): {model_name}")

    deleted = cleanup_stale_alarms(set(created))
    if deleted:
        print(f"Deleted stale alarms: {deleted}")

    build_dashboard(by_model, model_quotas)
    print(f"Updated dashboard: {DASHBOARD_NAME}")

    return {
        "upserted_alarms": len(created),
        "deleted_alarms": len(deleted),
        "skipped_no_quota": skipped,
        "dashboard": DASHBOARD_NAME,
        "models_with_quota": len(model_quotas),
        "models_total": len(by_model),
    }
