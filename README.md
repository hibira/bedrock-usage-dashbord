# Bedrock TPM Alarm Manager

Bedrock 推論プロファイルの `EstimatedTPMQuotaUsage` を監視する CloudWatch Alarm と Dashboard を日次で自動作成/更新する Lambda 関数。

## 機能

- `list-inference-profiles` で全推論プロファイルを取得
- ACTIVE なプロファイルごとに CloudWatch Alarm を作成/更新
- モデルごとにグループ化した CloudWatch Dashboard を作成/更新
- 削除済みプロファイルの古いアラームを自動クリーンアップ
- EventBridge で毎日 0:00 UTC (9:00 JST) に自動実行

## デプロイ

```bash
# 依存インストール
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# デプロイ（SNS トピック ARN を指定）
npx cdk deploy -c sns_topic_arn=arn:aws:sns:us-east-1:123456789012:your-topic
```

## Lambda 環境変数

| 変数名 | 説明 | デフォルト |
|--------|------|-----------|
| `SNS_TOPIC_ARN` | アラーム通知先 SNS トピック ARN | (必須) |
| `THRESHOLD_PERCENT` | アラームしきい値 (%) | `80` |
| `REGION` | AWS リージョン | `us-east-1` |
| `DASHBOARD_NAME` | ダッシュボード名 | `Bedrock-TPM-Usage` |
