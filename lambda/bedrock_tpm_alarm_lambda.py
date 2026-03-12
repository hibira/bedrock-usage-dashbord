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
                quota_type = "global" if is_global else "regional"
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
    quota_type: 'global' or 'regional'
    """
    model_keywords = _normalize(model_name.replace(".", " ").replace("-", " "))
    prefix = "global cross-region" if quota_type == "global" else "cross-region"

    best_match = None
    best_score = 0
    for qname_lower, qinfo in quotas.items():
        if prefix not in qname_lower:
            continue
        if quota_type == "regional" and "global" in qname_lower:
            continue
        if "1m context" in qname_lower:
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
    label = "Global" if quota_type == "global" else "Regional"
    return f"{model_name} [{label}]"


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

def _short_model_name(model_name):
    """Convert model ID to a human-readable short name.
    e.g. 'anthropic.claude-sonnet-4-6' -> 'Claude Sonnet 4.6'
         'anthropic.claude-opus-4-6-v1' -> 'Claude Opus 4.6'
    """
    name = model_name.split(".")[-1] if "." in model_name else model_name
    # Remove version suffixes
    name = re.sub(r"-\d{8}-v\d+:\d+$", "", name)
    name = re.sub(r"-v\d+(:\d+)?$", "", name)
    # Convert to parts and rebuild with dots for version numbers
    parts = name.split("-")
    result = []
    i = 0
    while i < len(parts):
        # Detect version pattern: two consecutive single digits
        if (i + 1 < len(parts) and parts[i].isdigit() and len(parts[i]) == 1
                and parts[i + 1].isdigit() and len(parts[i + 1]) == 1):
            result.append(f"{parts[i]}.{parts[i+1]}")
            i += 2
        else:
            result.append(parts[i].title())
            i += 1
    return " ".join(result)


def _build_metric_widget(key, profiles, quota, x, width, y):
    """Build a single metric widget for a (model, quota_type) group."""
    display = _display_name(key)
    quota_type = key[1]
    label = "Global" if quota_type == "global" else "Regional"

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
        return {
            "type": "metric", "x": x, "y": y, "width": width, "height": 6,
            "properties": {
                "metrics": raw_metrics,
                "view": "timeSeries", "region": REGION,
                "title": f"{label} (quota: {quota:,.0f} TPM)",
                "period": 60,
                "yAxis": {"left": {"min": 0, "max": 100, "label": "%"}},
                "annotations": {"horizontal": [
                    {"label": f"Threshold ({THRESHOLD_PERCENT}%)",
                     "value": THRESHOLD_PERCENT, "color": "#d62728"},
                ]},
            },
        }
    else:
        raw_metrics = [[
            "AWS/Bedrock", "EstimatedTPMQuotaUsage", "ModelId", pid,
            {"label": info["profile_name"], "stat": "Maximum"},
        ] for pid, info in profiles]
        return {
            "type": "metric", "x": x, "y": y, "width": width, "height": 6,
            "properties": {
                "metrics": raw_metrics,
                "view": "timeSeries", "region": REGION,
                "title": f"{label} (quota: unknown)",
                "period": 60,
            },
        }


def build_dashboard(by_model, model_quotas):
    """Create/update dashboard. Regional and Global graphs are placed side by side."""
    widgets = []
    y = 0

    widgets.append({
        "type": "text", "x": 0, "y": y, "width": 24, "height": 1,
        "properties": {
            "markdown": f"# Bedrock TPM Quota Usage (alarm threshold: {THRESHOLD_PERCENT}%)"
        },
    })
    y += 1

    # Group by model name, then lay out Regional (left) and Global (right)
    models = defaultdict(dict)
    for key, profiles in by_model.items():
        model_name, quota_type = key
        models[model_name][quota_type] = (key, profiles)

    for model_name in sorted(models.keys()):
        short = _short_model_name(model_name)
        widgets.append({
            "type": "text", "x": 0, "y": y, "width": 24, "height": 1,
            "properties": {"markdown": f"## {short}"},
        })
        y += 1

        types = models[model_name]
        if len(types) == 2:
            # Side by side: Regional (left 12), Global (right 12)
            for qt, x in [("regional", 0), ("global", 12)]:
                if qt in types:
                    key, profiles = types[qt]
                    quota = model_quotas.get(key)
                    widgets.append(_build_metric_widget(key, profiles, quota, x, 12, y))
            y += 6
        else:
            # Single type: full width
            for qt in types:
                key, profiles = types[qt]
                quota = model_quotas.get(key)
                widgets.append(_build_metric_widget(key, profiles, quota, 0, 24, y))
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
