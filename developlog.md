# 開發日誌（developlog）

> 專案：蝦皮廣告代操儀表板
> Repo：https://github.com/missparisbeauty/shopee-ad-dashboard
> 線上：GCP Cloud Run（URL/帳密見本機 HANDOFF.md，未上 public repo）

---

## 2026-05-17 / 18：CI/CD 自動化 + 多項功能強化

### A. 完整 CI/CD 自動化（GitHub Actions）
- `.github/workflows/ci.yml`：每次 push / PR 自動跑 123 個 pytest
- `.github/workflows/deploy.yml`：push 到 main → 測試通過 → 自動 Cloud Build + 部署 Cloud Run
- 建專用 service account `github-deployer`（最小權限：run.admin / cloudbuild.builds.editor / artifactregistry.writer / storage.admin / iam.serviceAccountUser）
- 金鑰存 GitHub repo secret（`GCP_SA_KEY` / `GCP_PROJECT`），本機金鑰用後即刪
- **連修 3 個「本機能跑、CI 環境不行」的差異**：
  1. `gcloud builds submit --no-cache` 需 kaniko（CI runner 無）→ 移除
  2. deployer SA 無權限 stream Cloud Build log → 加 `--suppress-logs`（無效）
  3. 改用 `--default-buckets-behavior=regional-user-owned-bucket`（自己 bucket 存 log）→ 成功

### B. 修 MPB 客戶
- 線上 customers.json（GCS）的 CU899 MPB 客戶 shop_ids 設為 `["MPB"]`
- 之後上傳 MPB 的 CSV 時，「店家」欄填 `MPB` 即自動歸戶

### C. 月報自動排程
- 新 endpoint `POST /api/v1/cron/monthly-reports`：自動產所有有資料客戶的「上月」月報並寄送
- auth：改用 `X-Cron-Token` header（非 Basic Auth），token 存 Secret Manager `cron-token`
- Cloud Scheduler job `monthly-reports-job`：每月 1 號 09:00（Asia/Taipei）觸發
- **修 bug**：Cloud Scheduler 預設 POST 無 body → GFE 回 HTTP 411 → 排程失敗。加 `--message-body` 解決，並透過 scheduler 真實路徑驗證成功
- SendGrid 未設定時為 mock（月報有產、只是不真寄）；設定後自動真寄

### D. 月報亮點/下月計畫改 AI 動態生成
- 原本固定模板 → 改呼叫 `ai_advisor.generate_insights`
- 有 ANTHROPIC_API_KEY 用 Claude 深入分析；沒有則用規則式分析（仍比死模板貼近真實數據）

### E. 商品健康度推估值標示更清楚
- 評分 / 評論 / 庫存 / 價競力 = 系統推估（蝦皮 CSV 無此資料）→ 灰字斜體 + `~值` + 表頭標「推估」
- 30 天銷量 / 健康度 = 基於真實 CSV → 正常顯示 + 表頭標「真實」
- 加黃底說明區塊解釋哪些可信、哪些僅供參考

### 其他
- **瀏覽器自動上傳腳本** `shopee-auto-upload.user.js`（Tampermonkey）：在使用者已登入的蝦皮 session 內攔截下載的廣告 CSV，自動傳到 dashboard。不存帳密、不違反 ToS、繞過反爬蟲（真人 session）。3 道攔截防線：fetch / XHR / blob download
- **全 UI 白話化**：Mock 模式→示範模式、partner_id→蝦皮合作夥伴編號、SendGrid→月報自動寄信、Claude→AI 深入分析、OAuth→授權連結 等，全部技術黑話改成非工程師看得懂的中文

---

## 2026-05-20：AI／寄信供應商切換 + 關鍵字／版位 CSV

> 本段為事後補記，依 commit `8beadc6`～`f35995f`（共 8 個，皆 2026-05-20，皆已自動部署上線）。

### F. AI 顧問改用 OpenAI gpt-4o（取代 Anthropic）
- `ai_advisor.py`：Anthropic SDK → OpenAI SDK，模型改用 gpt-4o
- `requirements.txt`：移除 `anthropic`，改加 `openai==1.82.0`
- `deploy.yml` 把 `OPENAI_API_KEY` secret 接進部署流程（commit `7aec2cc`）
- 過程中曾先把 Claude 模型升到 `claude-sonnet-4-6`（`416f975`），隨後整段換成 OpenAI

### G. 月報寄送改用 Gmail SMTP（取代 SendGrid）
- `email_client.py`：SendGrid API → Python 內建 `smtplib`（Gmail SMTP），不再需要額外套件
- `requirements.txt`：移除 `sendgrid`
- `deploy.yml` 接入 Gmail SMTP 相關 secrets（commit `3278443`）
- → 等於繞過 2026-05-18 HANDOFF 待辦 #2：原本卡在「進不了 GCP 758403173010 的 Secret Manager 設 Anthropic／SendGrid 金鑰」，改用可直接在 GitHub repo secret 設定的供應商解決

### H. 關鍵字／版位層級 CSV 支援
- parser 新增 `detect_report_type()`：分辨「總體」與「關鍵字／版位」兩種 CSV
- `COLUMN_ALIASES` 加 keyword（關鍵字／搜尋字／search keyword）+ placement（版位）
- store 新增 `aggregate_keywords()` / `aggregate_placements()`
- 新 API：`GET /api/v1/keywords/performance`、`GET /api/v1/placements/performance`
- UI：上傳區加「報表類型徽章」+ 5 期間說明（單日／週／月／3 月對應建議）；「關鍵字研究」頁加「✨ 真實關鍵字表現」「📍 版位表現」區塊（由真實 CSV 聚合）

### I. 修 CPC 報表解析 bug（commit `f35995f`）
- 補齊蝦皮 CPC 報表的欄位別名
- 修正「總體報表」被 `detect_report_type()` 誤判為「關鍵字／版位報表」的 bug

---

## 測試與品質

- pytest 共 **134 個**全綠（單元 + 整合 + edge case + auth/healthz；2026-05-20 新增 11 個 report type 偵測與新 endpoint 測試）
- 每次 push GitHub Actions 自動驗證
- Public repo 已驗證無敏感資料洩漏（客戶資料 / 密碼 / 金鑰 / HANDOFF 皆由 .gitignore 擋下）

---

## 已知限制 / 待辦

- 蝦皮 Open API 不開放廣告數據（CPC/ROAS/點擊）— 廣告數字只能靠 CSV
- 蝦皮 v4 公開 API 被反爬蟲擋（cloudscraper 也擋）→ 商品評分/庫存目前用推估，需申請 Shopee Partner API（5-10 工作天）
- 競品監控、高效時段熱力圖、關鍵字探索仍為示範資料（CSV 無此維度）
- AI 顧問已改 OpenAI gpt-4o、月報寄送已改 Gmail SMTP，secrets 皆接進 `deploy.yml`；未設定時仍保留 mock／規則式 fallback（2026-05-20 起，取代原 Anthropic／SendGrid）
- pytrends 被 Google 限流（daily_trends RSS 仍可用）

---

## 架構摘要

```
瀏覽器 → HTTPS（Cloud Run）→ HTTP Basic Auth（Secret Manager）
   → FastAPI（$PORT/$DATA_DIR）→ GCS bucket mount /data（JSON 持久化）
   min/max instance = 1（避免 JSON 併發寫衝突）

資料流：蝦皮廣告 CSV → 上傳/資料夾監看/瀏覽器腳本 → 解析聚合
   → KPI / 真實獲利 / 商品健康度 / 預算 / 月報 / AI 顧問
```

8 個核心 Python 模組：server / ad_report_parser / ad_data_store / csv_watcher /
rules_engine / ai_advisor / email_client / shopee_client（+ shopee_public_api /
google_trends_client / auth / paths）
