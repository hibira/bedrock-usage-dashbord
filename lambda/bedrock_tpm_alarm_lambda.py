"""
Auto-create/update CloudWatch Alarms and Dashboard for Bedrock inference profile
TPM (EstimatedTPMQuotaUsage) and RPM (Invocations Sum) metrics.
Alarms and dashboard are grouped by (model, quota_type: global/regional).
Thresholds are derived from actual Service Quotas. Runs daily via EventBridge.
"""

import json
import os
import re
from collections import defaultdict
from datetime import datetime, timezone

import boto3

REGION = os.environ.get("REGION", "us-east-1")
SNS_TOPIC_ARN = os.environ["SNS_TOPIC_ARN"]
THRESHOLD_PERCENT = float(os.environ.get("THRESHOLD_PERCENT", "80"))
ALARM_PREFIX_TPM = "Bedrock-TPM-"
ALARM_PREFIX_RPM = "Bedrock-RPM-"
DASHBOARD_NAME = os.environ.get("DASHBOARD_NAME", f"Bedrock-Quota-Usage-{REGION}")
MODEL_FILTER = [p.strip() for p in os.environ.get("MODEL_FILTER", "").split(",") if p.strip()]

bedrock = boto3.client("bedrock", region_name=REGION)
cw = boto3.client("cloudwatch", region_name=REGION)
sq = boto3.client("service-quotas", region_name=REGION)


# ---------------------------------------------------------------------------
# Inference profiles
# ---------------------------------------------------------------------------

def get_inference_profiles():
    profiles = {}
    paginator = bedrock.get_paginator("list_inference_profiles")
    for ptype in ("SYSTEM_DEFINED", "APPLICATION"):
        for page in paginator.paginate(typeEquals=ptype):
            for p in page["inferenceProfileSummaries"]:
                pid = p["inferenceProfileId"]
                model_arn = p.get("models", [{}])[0].get("modelArn", "unknown")
                model_name = model_arn.rsplit("/", 1)[-1] if "/" in model_arn else model_arn
                is_global = pid.startswith("global.") or ":bedrock:::" in model_arn
                profiles[pid] = {
                    "model_name": model_name,
                    "profile_name": p.get("inferenceProfileName", "") or pid,
                    "status": p.get("status"),
                    "quota_type": "global" if is_global else "regional",
                }
    return profiles


# ---------------------------------------------------------------------------
# Service Quotas
# ---------------------------------------------------------------------------

def get_quotas():
    """Fetch TPM and RPM quotas. Returns (tpm_quotas, rpm_quotas) dicts."""
    tpm, rpm = {}, {}
    paginator = sq.get_paginator("list_service_quotas")
    for page in paginator.paginate(ServiceCode="bedrock"):
        for q in page["Quotas"]:
            name = q["QuotaName"].lower()
            entry = {"code": q["QuotaCode"], "value": q["Value"], "name": q["QuotaName"]}
            if "tokens per minute" in name:
                tpm[name] = entry
            elif "requests per minute" in name:
                rpm[name] = entry
    return tpm, rpm


def _normalize(s):
    s = s.lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def match_quota(model_name, quota_type, quotas):
    model_keywords = _normalize(model_name.replace(".", " ").replace("-", " "))
    prefix = "global cross-region" if quota_type == "global" else "cross-region"
    best_match, best_score = None, 0
    for qname_lower, qinfo in quotas.items():
        if prefix not in qname_lower:
            continue
        if quota_type == "regional" and "global" in qname_lower:
            continue
        if "1m context" in qname_lower:
            continue
        score = sum(1 for w in model_keywords.split() if w in _normalize(qinfo["name"]))
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
    return any(p.lower() in model_name.lower() for p in MODEL_FILTER)


def _group_key(info):
    return (info["model_name"], info["quota_type"])


def _display_name(key):
    model_name, quota_type = key
    return f"{model_name} [{'Global' if quota_type == 'global' else 'Regional'}]"


def _alarm_suffix(key):
    model_name, quota_type = key
    return f"{model_name.replace('.', '-').replace(':', '-')}--{quota_type}"


def group_by_model(profiles):
    by_model = defaultdict(list)
    for pid, info in profiles.items():
        if info.get("status") == "ACTIVE" and _matches_filter(info["model_name"]):
            by_model[_group_key(info)].append((pid, info))
    return by_model


# ---------------------------------------------------------------------------
# CloudWatch Alarms (TPM + RPM)
# ---------------------------------------------------------------------------

def _put_alarm(prefix, metric_name, stat, key, profile_entries, quota_value, unit_label):
    alarm_name = f"{prefix}{_alarm_suffix(key)}"
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
                    "MetricName": metric_name,
                    "Dimensions": [{"Name": "ModelId", "Value": pid}],
                },
                "Period": 60,
                "Stat": stat,
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
            "Label": f"{display} Total {unit_label}",
            "ReturnData": True,
        })

    profile_names = ", ".join(info["profile_name"] for _, info in profile_entries)
    cw.put_metric_alarm(
        AlarmName=alarm_name,
        AlarmDescription=(
            f"Bedrock {unit_label} monitor: {display} | "
            f"quota: {quota_value:,.0f}, threshold: {threshold:,.0f} ({THRESHOLD_PERCENT}%) | "
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
            {"Key": "ManagedBy", "Value": "bedrock-quota-alarm-lambda"},
            {"Key": "ModelName", "Value": key[0]},
            {"Key": f"Quota{unit_label}", "Value": str(int(quota_value))},
        ],
    )
    return alarm_name


def cleanup_stale_alarms(prefix, active_names):
    paginator = cw.get_paginator("describe_alarms")
    stale = []
    for page in paginator.paginate(AlarmNamePrefix=prefix):
        for a in page["MetricAlarms"]:
            if a["AlarmName"] not in active_names:
                stale.append(a["AlarmName"])
    if stale:
        cw.delete_alarms(AlarmNames=stale)
    return stale


# ---------------------------------------------------------------------------
# CloudWatch Dashboard
# ---------------------------------------------------------------------------

def _short_model_name(model_name):
    name = model_name.split(".")[-1] if "." in model_name else model_name
    name = re.sub(r"-\d{8}-v\d+:\d+$", "", name)
    name = re.sub(r"-v\d+(:\d+)?$", "", name)
    parts = name.split("-")
    result = []
    i = 0
    while i < len(parts):
        if (i + 1 < len(parts) and parts[i].isdigit() and len(parts[i]) == 1
                and parts[i + 1].isdigit() and len(parts[i + 1]) == 1):
            result.append(f"{parts[i]}.{parts[i+1]}")
            i += 2
        else:
            result.append(parts[i].title())
            i += 1
    return " ".join(result)


def _build_metric_widget(metric_name, stat, unit_label, key, profiles, quota, x, width, y):
    quota_type = key[1]
    label = "Global" if quota_type == "global" else "Regional"
    title = f"{label} {unit_label}"
    if quota:
        title += f" (quota: {quota:,.0f})"
        raw_metrics = []
        expr_parts = []
        hide_individual = len(profiles) >= 5
        for i, (pid, info) in enumerate(profiles):
            mid = f"raw{i}"
            expr_parts.append(mid)
            raw_metrics.append([
                "AWS/Bedrock", metric_name, "ModelId", pid,
                {"id": mid, "visible": False, "stat": stat},
            ])
            raw_metrics.append([{
                "expression": f"{mid}/{quota}*100",
                "label": f"{info['profile_name']} (%)",
                "id": f"pct{i}",
                "visible": not hide_individual,
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
                "title": title, "period": 60,
                "yAxis": {"left": {"min": 0, "max": 100, "label": "%"}},
                "annotations": {"horizontal": [
                    {"label": f"Threshold ({THRESHOLD_PERCENT}%)",
                     "value": THRESHOLD_PERCENT, "color": "#ff7f0e"},
                ]},
            },
        }
    else:
        title += " (quota: unknown)"
        raw_metrics = [[
            "AWS/Bedrock", metric_name, "ModelId", pid,
            {"label": info["profile_name"], "stat": stat},
        ] for pid, info in profiles]
        return {
            "type": "metric", "x": x, "y": y, "width": width, "height": 6,
            "properties": {
                "metrics": raw_metrics,
                "view": "timeSeries", "region": REGION,
                "title": title, "period": 60,
                "yAxis": {"left": {"min": 0}},
            },
        }


def build_dashboard(by_model, tpm_quotas, rpm_quotas):
    """Build dashboard: per model, TPM row + RPM row, Regional left / Global right."""
    widgets = []
    y = 0

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    filter_text = f"`{', '.join(MODEL_FILTER)}`" if MODEL_FILTER else "all models"
    widgets.append({
        "type": "text", "x": 0, "y": y, "width": 24, "height": 2,
        "properties": {"markdown": (
            f"# Bedrock Quota Usage (TPM & RPM)\n"
            f"Alarm threshold: **{THRESHOLD_PERCENT}%** | "
            f"Models: **{len(set(k[0] for k in by_model))}** | "
            f"Filter: {filter_text} | "
            f"Updated: {now}"
        )},
    })
    y += 2

    models = defaultdict(dict)
    for key, profiles in by_model.items():
        model_name, quota_type = key
        models[model_name][quota_type] = (key, profiles)

    for model_name in sorted(models.keys()):
        short = _short_model_name(model_name)
        widgets.append({
            "type": "text", "x": 0, "y": y, "width": 24, "height": 1,
            "properties": {"markdown": f"---\n## {short}"},
        })
        y += 1

        types = models[model_name]
        has_both = len(types) == 2

        # TPM row
        for qt, x in ([("regional", 0), ("global", 12)] if has_both else [(list(types.keys())[0], 0)]):
            if qt not in types:
                continue
            key, profiles = types[qt]
            w = 12 if has_both else 24
            widgets.append(_build_metric_widget(
                "EstimatedTPMQuotaUsage", "Maximum", "TPM",
                key, profiles, tpm_quotas.get(key), x, w, y,
            ))
        y += 6

        # RPM row
        for qt, x in ([("regional", 0), ("global", 12)] if has_both else [(list(types.keys())[0], 0)]):
            if qt not in types:
                continue
            key, profiles = types[qt]
            w = 12 if has_both else 24
            widgets.append(_build_metric_widget(
                "Invocations", "Sum", "RPM",
                key, profiles, rpm_quotas.get(key), x, w, y,
            ))
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

    tpm_quotas_raw, rpm_quotas_raw = get_quotas()
    print(f"Found {len(tpm_quotas_raw)} TPM quotas, {len(rpm_quotas_raw)} RPM quotas")

    by_model = group_by_model(profiles)
    print(f"Grouped into {len(by_model)} groups")

    tpm_quotas, rpm_quotas = {}, {}
    for key in by_model:
        model_name, quota_type = key
        tpm_val = match_quota(model_name, quota_type, tpm_quotas_raw)
        rpm_val = match_quota(model_name, quota_type, rpm_quotas_raw)
        if tpm_val:
            tpm_quotas[key] = tpm_val
        if rpm_val:
            rpm_quotas[key] = rpm_val
    print(f"Matched TPM: {len(tpm_quotas)}, RPM: {len(rpm_quotas)} / {len(by_model)} groups")

    created_tpm, created_rpm, skipped = [], [], []
    for key, entries in by_model.items():
        tpm_q = tpm_quotas.get(key)
        rpm_q = rpm_quotas.get(key)
        if tpm_q:
            created_tpm.append(_put_alarm(
                ALARM_PREFIX_TPM, "EstimatedTPMQuotaUsage", "Maximum",
                key, entries, tpm_q, "TPM",
            ))
        if rpm_q:
            created_rpm.append(_put_alarm(
                ALARM_PREFIX_RPM, "Invocations", "Sum",
                key, entries, rpm_q, "RPM",
            ))
        if not tpm_q and not rpm_q:
            skipped.append(_display_name(key))

    del_tpm = cleanup_stale_alarms(ALARM_PREFIX_TPM, set(created_tpm))
    del_rpm = cleanup_stale_alarms(ALARM_PREFIX_RPM, set(created_rpm))

    build_dashboard(by_model, tpm_quotas, rpm_quotas)
    print(f"Updated dashboard: {DASHBOARD_NAME}")

    return {
        "tpm_alarms": len(created_tpm),
        "rpm_alarms": len(created_rpm),
        "deleted_tpm": len(del_tpm),
        "deleted_rpm": len(del_rpm),
        "skipped": skipped,
        "dashboard": DASHBOARD_NAME,
        "groups": len(by_model),
    }
