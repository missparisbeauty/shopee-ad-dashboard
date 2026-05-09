#!/bin/bash
# 蝦皮代操儀表板 — Cloud Run 一鍵部署腳本（v3.1：用 Secret Manager 管密碼）
#
# 用法：
#   1. 第一次部署前先跑 setup-gcp.sh（建 bucket / API / secrets）
#   2. ./deploy.sh
#
# 改動：v3.0 用 --set-env-vars 直接塞密碼（Cloud Run console 看得到、log 也可能洩漏）
#       v3.1 改用 --set-secrets 從 Secret Manager 取，更安全
#
# 需要先裝好 gcloud CLI 並登入：
#   gcloud auth login
#   gcloud config set project YOUR_PROJECT_ID

set -euo pipefail

# ─────────────────────────── 設定（改成你的）───────────────────────────
PROJECT_ID="${PROJECT_ID:-shopee-dashboard-$(whoami)}"
REGION="${REGION:-asia-east1}"
SERVICE="${SERVICE:-shopee-dashboard}"
BUCKET="${BUCKET:-${PROJECT_ID}-data}"

# Secret 名稱（在 setup-gcp.sh 已建立）
SECRET_AUTH_PASS="${SECRET_AUTH_PASS:-shopee-dashboard-auth-pass}"
SECRET_ANTHROPIC="${SECRET_ANTHROPIC:-anthropic-api-key}"
SECRET_SENDGRID="${SECRET_SENDGRID:-sendgrid-api-key}"
SECRET_SHOPEE_PARTNER="${SECRET_SHOPEE_PARTNER:-shopee-partner-key}"

# 非敏感環境變數
BASIC_AUTH_USER="${BASIC_AUTH_USER:-admin}"
SENDGRID_FROM_EMAIL="${SENDGRID_FROM_EMAIL:-}"
SHOPEE_PARTNER_ID="${SHOPEE_PARTNER_ID:-}"

# ─────────────────────────── 檢查 secret 存在 ───────────────────────────

check_secret() {
    local name="$1"
    if gcloud secrets describe "${name}" --project="${PROJECT_ID}" &>/dev/null; then
        return 0
    else
        return 1
    fi
}

echo "▶ 部署到 GCP project: ${PROJECT_ID}, region: ${REGION}"

# 確認必要的 secret 存在
if ! check_secret "${SECRET_AUTH_PASS}"; then
    echo "✗ Secret '${SECRET_AUTH_PASS}' 不存在！"
    echo "  請先跑 setup-gcp.sh 建立 secret，或手動建立："
    echo "    echo -n 'YourStrongPassword' | gcloud secrets create ${SECRET_AUTH_PASS} --data-file=- --project=${PROJECT_ID}"
    exit 1
fi
echo "✓ Secret '${SECRET_AUTH_PASS}' 存在"

# ─────────────────────────── 部署 ───────────────────────────

# 1. Build container（用 Cloud Build，不用本機 docker）
echo "[1/3] 建 image..."
gcloud builds submit \
    --project="${PROJECT_ID}" \
    --tag="${REGION}-docker.pkg.dev/${PROJECT_ID}/cloud-run-source-deploy/${SERVICE}" \
    .

# 2. 組合非敏感環境變數
ENV_VARS="DATA_DIR=/data,BASIC_AUTH_USER=${BASIC_AUTH_USER}"
[ -n "${SENDGRID_FROM_EMAIL}" ] && ENV_VARS="${ENV_VARS},SENDGRID_FROM_EMAIL=${SENDGRID_FROM_EMAIL}"
[ -n "${SHOPEE_PARTNER_ID}" ]   && ENV_VARS="${ENV_VARS},SHOPEE_PARTNER_ID=${SHOPEE_PARTNER_ID}"

# 3. 組合 Secrets（敏感資料從 Secret Manager 取）
SECRETS="BASIC_AUTH_PASS=${SECRET_AUTH_PASS}:latest"
if check_secret "${SECRET_ANTHROPIC}"; then
    SECRETS="${SECRETS},ANTHROPIC_API_KEY=${SECRET_ANTHROPIC}:latest"
fi
if check_secret "${SECRET_SENDGRID}"; then
    SECRETS="${SECRETS},SENDGRID_API_KEY=${SECRET_SENDGRID}:latest"
fi
if check_secret "${SECRET_SHOPEE_PARTNER}"; then
    SECRETS="${SECRETS},SHOPEE_PARTNER_KEY=${SECRET_SHOPEE_PARTNER}:latest"
fi

# 4. 部署到 Cloud Run（含 GCS bucket mount + Secret Manager）
echo "[2/3] 部署 Cloud Run service..."
gcloud run deploy "${SERVICE}" \
    --project="${PROJECT_ID}" \
    --region="${REGION}" \
    --image="${REGION}-docker.pkg.dev/${PROJECT_ID}/cloud-run-source-deploy/${SERVICE}" \
    --platform=managed \
    --allow-unauthenticated \
    --port=8080 \
    --memory=512Mi \
    --cpu=1 \
    --min-instances=1 \
    --max-instances=1 \
    --timeout=300 \
    --concurrency=10 \
    --set-env-vars="${ENV_VARS}" \
    --set-secrets="${SECRETS}" \
    --add-volume=name=data-volume,type=cloud-storage,bucket="${BUCKET}" \
    --add-volume-mount=volume=data-volume,mount-path=/data \
    --execution-environment=gen2

# 5. 印 URL
echo
echo "[3/3] ✅ 部署完成！"
URL=$(gcloud run services describe "${SERVICE}" --project="${PROJECT_ID}" --region="${REGION}" --format='value(status.url)')
echo "  URL  : ${URL}"
echo "  帳號 : ${BASIC_AUTH_USER}"
echo "  密碼 : (存在 Secret Manager: ${SECRET_AUTH_PASS})"
echo
echo "瀏覽器打開：${URL}"
echo
echo "看密碼："
echo "  gcloud secrets versions access latest --secret=${SECRET_AUTH_PASS} --project=${PROJECT_ID}"
echo
echo "改密碼："
echo "  echo -n 'NewPassword' | gcloud secrets versions add ${SECRET_AUTH_PASS} --data-file=- --project=${PROJECT_ID}"
echo "  ./deploy.sh   # 重新部署讓新版本生效"
