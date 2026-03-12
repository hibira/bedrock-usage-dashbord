"""
Auto-create/update CloudWatch Alarms and Dashboard for Bedrock inference profile
EstimatedTPMQuotaUsage metrics. Alarms and dashboard graphs are grouped by
(model_name, quota_type) where quota_type is 'global' or 'regional'.
Thresholds are derived from actual Service Quotas. Runs daily via EventBridge.
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
# Comma-separated model name patterns (partial match). Empty = all models.
MODEL_FILTER = [p.strip() for p in os.environ.get("MODEL_FILTER", "").split(",") if p.strip()]

bedrock = boto3.client("bedrock", region_name=REGION)
cw = boto3.client("cloudwatch", region_name=REGION)
sq = boto3.client("service-quotas", region_name=REGION)


# ---------------------------------------------------------------------------
# Inference profiles
# ---------------------------------------------------------------------------

def get_inference_profiles():
    """Fetch both SYSTEM_DEFINED and APPLICATION inference profiles."""
    profiles = {}
    paginator = bedrock.get_paginator("list_inference_profiles")
    for ptype in ("SYSTEM_DEFINED", "APPLICATION"):
        for page in paginator.paginate(typeEquals=ptype):
            for p in page["inferenceProfileSummaries"]:
                pid = p["inferenceProfileId"]
                model_arn = p.get("models", [{}])[0].get("modelArn", "unknown")
                model_name = model_arn.rsplit("/", 1)[-1] if "/" in model_arn else model_arn
                name = p.get("inferenceProfileName", "")
                # Global: system profile starting with "global." OR
                #         application profile whose model ARN has no region
                #         (arn:aws:bedrock:::foundation-model/...)
                is_global = pid.startswith("global.") or ":bedrock:::" in model_arn
                is_1m = "1m" in pid.lower() or "1m" in name.lower() or "1m" in model_name.lower()
                if is_global and is_1m:
                    quota_type = "global-1m"
                elif is_global:
                    quota_type = "global"
                elif is_1m:
                    quota_type = "regional-1m"
                else:
                    quota_type = "regional"
                profiles[pid] = {
                    "model_name": model_name,
                    "profile_name": name or pid,
                    "status": p.get("status"),
                    "quota_type": quota_type,
                }
    return profiles


# ---------------------------------------------------------------------------
# Service Quotas
# ---------------------------------------------------------------------------

def get_tpm_quotas():
    """Fetch all Bedrock TPM quotas from Service Quotas."""
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
    s = s.lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def match_quota(model_name, quota_type, quotas):
    """Find the best matching TPM quota for a model + quota_type.
    quota_type: global/global-1m/regional/regional-1m
    """
    model_keywords = _normalize(model_name.replace(".", " ").replace("-", " "))
    is_1m = quota_type.endswith("-1m")
    is_global = quota_type.startswith("global")
    prefix = "global cross-region" if is_global else "cross-region"

    best_match = None
    best_score = 0
    for qname_lower, qinfo in quotas.items():
        if prefix not in qname_lower:
            continue
        if not is_global and "global" in qname_lower:
            continue
        # 1M context matching
        has_1m = "1m context" in qname_lower
        if is_1m != has_1m:
            continue

        q_normalized = _normalize(qinfo["name"])
        score = sum(1 for w in model_keywords.split() if w in q_normalized)
        if score > best_score:
            best_score = score
            best_match = qinfo

    return best_match["value"] if best_match and best_score >= 2 else None


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------

def _matches_filter(model_name):
    if not MODEL_FILTER:
        return True
    name_lower = model_name.lower()
    return any(p.lower() in name_lower for p in MODEL_FILTER)


def _group_key(info):
    return (info["model_name"], info["quota_type"])


def _display_name(key):
    model_name, quota_type = key
    labels = {
        "global": "Global",
        "global-1m": "Global 1M",
        "regional": "Regional",
        "regional-1m": "Regional 1M",
    }
    return f"{model_name} [{labels.get(quota_type, quota_type)}]"


def _alarm_suffix(key):
    model_name, quota_type = key
    safe = model_name.replace(".", "-").replace(":", "-")
    return f"{safe}--{quota_type}"


def group_by_model(profiles):
    """Group active profiles by (model_name, quota_type), applying MODEL_FILTER."""
    by_model = defaultdict(list)
    for pid, info in profiles.items():
        if info.get("status") == "ACTIVE" and _matches_filter(info["model_name"]):
            by_model[_group_key(info)].append((pid, info))
    return by_model


# ---------------------------------------------------------------------------
# CloudWatch Alarms
# ---------------------------------------------------------------------------

def put_model_alarm(key, profile_entries, quota_value):
    """Create/update a CloudWatch Alarm with threshold = quota * THRESHOLD_PERCENT%."""
    alarm_name = f"{ALARM_PREFIX}{_alarm_suffix(key)}"
    display = _display_name(key)
    threshold = quota_value * THRESHOLD_PERCENT / 100

    metrics = []
    metric_ids = []
    for i, (pid, _) in enumerate(profile_entries):
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
            "Label": f"{display} Total TPM",
            "ReturnData": True,
        })

    profile_names = ", ".join(info["profile_name"] for _, info in profile_entries)
    cw.put_metric_alarm(
        AlarmName=alarm_name,
        AlarmDescription=(
            f"Bedrock TPM monitor: {display} | "
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
            {"Key": "ModelName", "Value": key[0]},
            {"Key": "QuotaTPM", "Value": str(int(quota_value))},
        ],
    )
    return alarm_name


def cleanup_stale_alarms(active_alarm_names):
    paginator = cw.get_paginator("describe_alarms")
    stale = []
    for page in paginator.paginate(AlarmNamePrefix=ALARM_PREFIX):
        for a in page["MetricAlarms"]:
            if a["AlarmName"] not in active_alarm_names:
                stale.append(a["AlarmName"])
    if stale:
        cw.delete_alarms(AlarmNames=stale)
    return stale


# ---------------------------------------------------------------------------
# CloudWatch Dashboard
# ---------------------------------------------------------------------------

def build_dashboard(by_model, model_quotas):
    """Create/update dashboard. Each (model, quota_type) group gets its own graph."""
    widgets = []
    y = 0

    widgets.append({
        "type": "text", "x": 0, "y": y, "width": 24, "height": 1,
        "properties": {
            "markdown": f"# Bedrock TPM Quota Usage (alarm threshold: {THRESHOLD_PERCENT}%)"
        },
    })
    y += 1

    for key, profiles in sorted(by_model.items(), key=lambda x: _display_name(x[0])):
        display = _display_name(key)
        quota = model_quotas.get(key)
        quota_label = f" (quota: {quota:,.0f} TPM)" if quota else " (quota: unknown)"

        widgets.append({
            "type": "text", "x": 0, "y": y, "width": 24, "height": 1,
            "properties": {"markdown": f"## {display}{quota_label}"},
        })
        y += 1

        if quota:
            raw_metrics = []
            expr_parts = []
            for i, (pid, info) in enumerate(profiles):
                mid = f"raw{i}"
                expr_parts.append(mid)
                raw_metrics.append([
                    "AWS/Bedrock", "EstimatedTPMQuotaUsage", "ModelId", pid,
                    {"id": mid, "visible": False, "stat": "Maximum"},
                ])
                raw_metrics.append([{
                    "expression": f"{mid}/{quota}*100",
                    "label": f"{info['profile_name']} (%)",
                    "id": f"pct{i}",
                }])
            raw_metrics.append([{
                "expression": f"({'+'.join(expr_parts)})/{quota}*100",
                "label": "Total (%)",
                "id": "total_pct",
                "color": "#d62728",
            }])
            widgets.append({
                "type": "metric", "x": 0, "y": y, "width": 24, "height": 6,
                "properties": {
                    "metrics": raw_metrics,
                    "view": "timeSeries", "region": REGION,
                    "title": f"{display} - TPM Quota Usage (%)",
                    "period": 60,
                    "yAxis": {"left": {"min": 0, "max": 100, "label": "%"}},
                    "annotations": {"horizontal": [
                        {"label": f"Threshold ({THRESHOLD_PERCENT}%)",
                         "value": THRESHOLD_PERCENT, "color": "#d62728"},
                    ]},
                },
            })
        else:
            raw_metrics = [[
                "AWS/Bedrock", "EstimatedTPMQuotaUsage", "ModelId", pid,
                {"label": info["profile_name"], "stat": "Maximum"},
            ] for pid, info in profiles]
            widgets.append({
                "type": "metric", "x": 0, "y": y, "width": 24, "height": 6,
                "properties": {
                    "metrics": raw_metrics,
                    "view": "timeSeries", "region": REGION,
                    "title": f"{display} - TPM (raw, quota unknown)",
                    "period": 60,
                },
            })
        y += 6

    cw.put_dashboard(
        DashboardName=DASHBOARD_NAME,
        DashboardBody=json.dumps({"widgets": widgets}),
    )


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

def handler(event, context):
    profiles = get_inference_profiles()
    print(f"Found {len(profiles)} inference profiles")

    quotas = get_tpm_quotas()
    print(f"Found {len(quotas)} TPM quotas")

    by_model = group_by_model(profiles)
    print(f"Grouped into {len(by_model)} groups")

    model_quotas = {}
    for key in by_model:
        model_name, quota_type = key
        val = match_quota(model_name, quota_type, quotas)
        if val:
            model_quotas[key] = val
    print(f"Matched quotas for {len(model_quotas)}/{len(by_model)} groups")

    created = []
    skipped = []
    for key, entries in by_model.items():
        quota = model_quotas.get(key)
        if quota:
            name = put_model_alarm(key, entries, quota)
            created.append(name)
            print(f"Upserted: {name} (quota={quota:,.0f}, threshold={quota*THRESHOLD_PERCENT/100:,.0f})")
        else:
            skipped.append(_display_name(key))
            print(f"Skipped: {_display_name(key)}")

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
        "groups_with_quota": len(model_quotas),
        "groups_total": len(by_model),
    }
