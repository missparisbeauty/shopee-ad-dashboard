#!/bin/bash
# 第一次部署前的初始化 — 建 GCP project / Bucket / Secrets / 啟用 APIs
# v3.1: 加 Secret Manager 支援
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-shopee-dashboard-$(whoami)}"
REGION="${REGION:-asia-east1}"
BUCKET="${BUCKET:-${PROJECT_ID}-data}"

# Secret 名稱（會建在 Secret Manager）
SECRET_AUTH_PASS="${SECRET_AUTH_PASS:-shopee-dashboard-auth-pass}"
SECRET_ANTHROPIC="${SECRET_ANTHROPIC:-anthropic-api-key}"
SECRET_SENDGRID="${SECRET_SENDGRID:-sendgrid-api-key}"
SECRET_SHOPEE_PARTNER="${SECRET_SHOPEE_PARTNER:-shopee-partner-key}"

echo "▶ 初始化 GCP project: ${PROJECT_ID}"
echo

# 1. 建 project（如果還沒）
if ! gcloud projects describe "${PROJECT_ID}" &>/dev/null; then
    echo "[1/6] 建立 GCP project..."
    gcloud projects create "${PROJECT_ID}" --name="蝦皮代操儀表板"
    echo "  ⚠️  請去 console 啟用計費後再繼續：https://console.cloud.google.com/billing/linkedaccount?project=${PROJECT_ID}"
    read -p "  啟用計費後按 Enter 繼續..."
fi

gcloud config set project "${PROJECT_ID}"

# 2. 啟用必要的 APIs
echo "[2/6] 啟用 APIs..."
gcloud services enable \
    run.googleapis.com \
    cloudbuild.googleapis.com \
    artifactregistry.googleapis.com \
    storage.googleapis.com \
    secretmanager.googleapis.com

# 3. 建 GCS bucket
echo "[3/6] 建立 GCS bucket: gs://${BUCKET}/..."
if ! gsutil ls -b "gs://${BUCKET}" &>/dev/null; then
    gsutil mb -l "${REGION}" -p "${PROJECT_ID}" "gs://${BUCKET}"
fi

# 4. 上傳本機資料（如果有的話）
echo "[4/6] 上傳本機 JSON 資料到 GCS..."
for f in ad_reports.json customers.json profit_config.json rules.json snapshots.json \
         shopee_connections.json shopee_products.json watcher_state.json \
         report_log.json adjustments.json competitors.json audiences.json \
         team.json notify_config.json uploads_index.json; do
    [ -f "$f" ] && gsutil cp "$f" "gs://${BUCKET}/" || true
done
# 建 watch / uploads 子資料夾結構
echo "" | gsutil cp - "gs://${BUCKET}/watch/.keep"
echo "" | gsutil cp - "gs://${BUCKET}/uploads/.keep"

# 5. 建 Artifact Registry repository
echo "[5/6] 建立 Artifact Registry..."
if ! gcloud artifacts repositories describe cloud-run-source-deploy \
        --location="${REGION}" --project="${PROJECT_ID}" &>/dev/null; then
    gcloud artifacts repositories create cloud-run-source-deploy \
        --repository-format=docker \
        --location="${REGION}" \
        --project="${PROJECT_ID}"
fi

# 6. 建立 Secrets（Secret Manager）
echo "[6/6] 建立 Secrets..."

create_or_skip_secret() {
    local name="$1"
    local value="$2"
    if gcloud secrets describe "${name}" --project="${PROJECT_ID}" &>/dev/null; then
        echo "  - Secret '${name}' 已存在（跳過）"
    else
        if [ -z "${value}" ]; then
            echo "  - Secret '${name}' 沒給值（跳過建立，之後手動建）"
            return
        fi
        echo -n "${value}" | gcloud secrets create "${name}" \
            --replication-policy=automatic \
            --data-file=- \
            --project="${PROJECT_ID}"
        echo "  ✓ 已建立 secret '${name}'"
    fi
}

# BASIC_AUTH_PASS — 必填
if [ -z "${BASIC_AUTH_PASS:-}" ]; then
    echo
    echo "  ❓ 請輸入 dashboard 登入密碼（不會顯示）"
    read -s -p "     BASIC_AUTH_PASS: " BASIC_AUTH_PASS
    echo
    if [ -z "${BASIC_AUTH_PASS}" ]; then
        echo "  ✗ 密碼不能空，請重跑 setup-gcp.sh"
        exit 1
    fi
fi
create_or_skip_secret "${SECRET_AUTH_PASS}" "${BASIC_AUTH_PASS}"

# ANTHROPIC_API_KEY — 選填
create_or_skip_secret "${SECRET_ANTHROPIC}" "${ANTHROPIC_API_KEY:-}"
create_or_skip_secret "${SECRET_SENDGRID}" "${SENDGRID_API_KEY:-}"
create_or_skip_secret "${SECRET_SHOPEE_PARTNER}" "${SHOPEE_PARTNER_KEY:-}"

# 給 Cloud Run service account 讀 secret 的權限
echo "  授予 Cloud Run service account 讀 secret 的權限..."
PROJECT_NUMBER=$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')
SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
for secret in "${SECRET_AUTH_PASS}" "${SECRET_ANTHROPIC}" "${SECRET_SENDGRID}" "${SECRET_SHOPEE_PARTNER}"; do
    if gcloud secrets describe "${secret}" --project="${PROJECT_ID}" &>/dev/null; then
        gcloud secrets add-iam-policy-binding "${secret}" \
            --member="serviceAccount:${SA}" \
            --role=roles/secretmanager.secretAccessor \
            --project="${PROJECT_ID}" \
            --quiet >/dev/null 2>&1 || true
    fi
done

echo
echo "✅ GCP 初始化完成！"
echo "  PROJECT  : ${PROJECT_ID}"
echo "  BUCKET   : gs://${BUCKET}"
echo "  REGION   : ${REGION}"
echo "  SECRETS  : ${SECRET_AUTH_PASS} (BASIC_AUTH_PASS)"
echo "             ${SECRET_ANTHROPIC} (Anthropic, 如有設)"
echo "             ${SECRET_SENDGRID} (SendGrid, 如有設)"
echo "             ${SECRET_SHOPEE_PARTNER} (Shopee Partner, 如有設)"
echo
echo "下一步：./deploy.sh"
echo
echo "之後改密碼："
echo "  echo -n 'NewPassword' | gcloud secrets versions add ${SECRET_AUTH_PASS} --data-file=-"
echo "  ./deploy.sh   # 重新部署讓新版本生效"
