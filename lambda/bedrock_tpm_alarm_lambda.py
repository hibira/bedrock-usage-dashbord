"""
Bedrock 推論プロファイルの EstimatedTPMQuotaUsage を監視する
CloudWatch Alarm + Dashboard を日次で自動作成/更新する Lambda 関数。
"""

import json
import os
from collections import defaultdict

import boto3

REGION = os.environ.get("REGION", "us-east-1")
SNS_TOPIC_ARN = os.environ["SNS_TOPIC_ARN"]
THRESHOLD_PERCENT = float(os.environ.get("THRESHOLD_PERCENT", "80"))
ALARM_PREFIX = "Bedrock-TPM-"
DASHBOARD_NAME = os.environ.get("DASHBOARD_NAME", "Bedrock-TPM-Usage")

bedrock = boto3.client("bedrock", region_name=REGION)
cw = boto3.client("cloudwatch", region_name=REGION)


def get_inference_profiles():
    """推論プロファイル一覧を取得し、ID→情報のマッピングを返す。"""
    profiles = {}
    paginator = bedrock.get_paginator("list_inference_profiles")
    for page in paginator.paginate():
        for p in page["inferenceProfileSummaries"]:
            profile_id = p["inferenceProfileId"]
            model_arn = p.get("models", [{}])[0].get("modelArn", "unknown")
            model_name = model_arn.rsplit("/", 1)[-1] if "/" in model_arn else model_arn
            profiles[profile_id] = {
                "model_name": model_name,
                "profile_name": p.get("inferenceProfileName", profile_id),
                "status": p.get("status"),
            }
    return profiles


def put_alarm(profile_id, info):
    alarm_name = f"{ALARM_PREFIX}{profile_id}"
    cw.put_metric_alarm(
        AlarmName=alarm_name,
        AlarmDescription=(
            f"Bedrock TPM 監視: {info['profile_name']} "
            f"(model: {info['model_name']}, profile: {profile_id})"
        ),
        Namespace="AWS/Bedrock",
        MetricName="EstimatedTPMQuotaUsage",
        Dimensions=[{"Name": "ModelId", "Value": profile_id}],
        Statistic="Maximum",
        Period=60,
        EvaluationPeriods=1,
        Threshold=THRESHOLD_PERCENT,
        ComparisonOperator="GreaterThanOrEqualToThreshold",
        TreatMissingData="notBreaching",
        AlarmActions=[SNS_TOPIC_ARN],
        OKActions=[SNS_TOPIC_ARN],
        Tags=[
            {"Key": "ManagedBy", "Value": "bedrock-tpm-alarm-lambda"},
            {"Key": "ModelName", "Value": info["model_name"]},
        ],
    )
    return alarm_name


def cleanup_stale_alarms(active_profile_ids):
    paginator = cw.get_paginator("describe_alarms")
    stale = []
    for page in paginator.paginate(AlarmNamePrefix=ALARM_PREFIX):
        for a in page["MetricAlarms"]:
            pid = a["AlarmName"].removeprefix(ALARM_PREFIX)
            if pid not in active_profile_ids:
                stale.append(a["AlarmName"])
    if stale:
        cw.delete_alarms(AlarmNames=stale)
    return stale


def build_dashboard(active_profiles):
    """モデルごとにグループ化した CloudWatch Dashboard を作成/更新する。"""
    # モデル名でグループ化
    by_model = defaultdict(list)
    for pid, info in active_profiles.items():
        by_model[info["model_name"]].append((pid, info))

    widgets = []
    y = 0

    # タイトル
    widgets.append({
        "type": "text",
        "x": 0, "y": y, "width": 24, "height": 1,
        "properties": {
            "markdown": f"# Bedrock TPM Quota Usage (threshold: {THRESHOLD_PERCENT}%)"
        },
    })
    y += 1

    for model_name, profiles in sorted(by_model.items()):
        # モデル名ヘッダー
        widgets.append({
            "type": "text",
            "x": 0, "y": y, "width": 24, "height": 1,
            "properties": {"markdown": f"## {model_name}"},
        })
        y += 1

        # 該当モデルの全プロファイルを1つのグラフにまとめる
        metrics = []
        for pid, info in profiles:
            metrics.append([
                "AWS/Bedrock", "EstimatedTPMQuotaUsage",
                "ModelId", pid,
                {"label": info["profile_name"], "stat": "Maximum"},
            ])

        widgets.append({
            "type": "metric",
            "x": 0, "y": y, "width": 24, "height": 6,
            "properties": {
                "metrics": metrics,
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
        y += 6

    cw.put_dashboard(
        DashboardName=DASHBOARD_NAME,
        DashboardBody=json.dumps({"widgets": widgets}),
    )


def handler(event, context):
    profiles = get_inference_profiles()
    print(f"Found {len(profiles)} inference profiles")

    active = {}
    created = []
    for profile_id, info in profiles.items():
        if info.get("status") == "ACTIVE":
            name = put_alarm(profile_id, info)
            created.append(name)
            active[profile_id] = info
            print(f"Upserted alarm: {name} -> {info['model_name']}")

    deleted = cleanup_stale_alarms(set(active.keys()))
    if deleted:
        print(f"Deleted stale alarms: {deleted}")

    build_dashboard(active)
    print(f"Updated dashboard: {DASHBOARD_NAME}")

    return {
        "upserted_alarms": len(created),
        "deleted_alarms": len(deleted),
        "dashboard": DASHBOARD_NAME,
        "models": list({i["model_name"] for i in active.values()}),
    }
