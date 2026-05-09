# 部署到 GCP Cloud Run

> **方案 A：MVP（你自己用，HTTP Basic Auth + Secret Manager）**
> 預估時間 30-60 分鐘（第一次） · 之後每次更新部署 3-5 分鐘
> 預估費用 $0/月（在免費額度內）
>
> **v3.1 改用 Secret Manager 保管密碼**（不再明文塞 env vars）

---

## 架構

```
你的瀏覽器
    ↓ HTTPS（Cloud Run 自動）
    ↓ HTTP Basic Auth（一道防線，避免公網裸奔）
    ↓
Cloud Run service: shopee-dashboard
    ├─ container 跑 server.py（吃 $PORT, $DATA_DIR）
    ├─ min/max instance = 1（避免併發寫衝突）
    └─ 掛 GCS bucket → /data（持久化所有 JSON）
        ├─ ad_reports.json
        ├─ customers.json
        ├─ profit_config.json
        └─ watch/ uploads/ 等
```

**為什麼 min=max=1？**
JSON 檔當資料庫 + GCS volume mount → 多 instance 同時寫會壞檔。代操業者一個人/一團隊用，1 instance 完全夠（Cloud Run 1 instance 處理上百並發 request 沒問題）。

---

## 前置要求

1. **GCP 帳號**（[console.cloud.google.com](https://console.cloud.google.com)，免費 $300 額度）
2. **gcloud CLI** [安裝指南](https://cloud.google.com/sdk/docs/install) — Windows 推薦下載 installer
3. **登入**：
   ```bash
   gcloud auth login
   ```

---

## 部署步驟

### Step 1: 第一次初始化（只跑一次）— 含 Secret Manager 設定

```bash
cd C:\Users\winuser\Desktop\蝦皮代操儀表板

# 可選：先 export API key（會被存進 Secret Manager；沒給就跳過該 secret）
export ANTHROPIC_API_KEY='sk-ant-...'         # AI 顧問
export SENDGRID_API_KEY='SG....'              # 月報寄信
export SHOPEE_PARTNER_KEY='your-key'          # 蝦皮 Partner

# 在 Git Bash 跑（Windows 的 .sh 要 bash 環境）
bash setup-gcp.sh
# 過程中會提示輸入 BASIC_AUTH_PASS（不會在螢幕顯示，安全）
```

腳本會：
1. 建 GCP project（你會被提示去 console 啟用計費 — 這步必須做才能繼續）
2. 啟用 6 個 APIs（Cloud Run / Build / Storage / Artifact Registry / Secret Manager 等）
3. 建 GCS bucket（資料 mount 到 Cloud Run 用）
4. 上傳你目前所有 JSON 資料到 GCS
5. 建 Artifact Registry repository
6. **建 Secret Manager secrets**（密碼存這裡，不會出現在 env / logs）
7. 自動授權 Cloud Run service account 讀取 secrets

### Step 2: 部署

```bash
# 不需要 export 密碼了，已存在 Secret Manager
bash deploy.sh
```

完成後會印 URL，例如：
```
URL  : https://shopee-dashboard-xxx-de.a.run.app
帳號 : admin
密碼 : YourStrongPasswordHere123!
```

打開瀏覽器 → 輸入帳密 → dashboard 就活了。

---

## 之後每次更新（改 code 後重新部署）

```bash
bash deploy.sh   # 密碼已存 Secret Manager，不需要 export
```

3-5 分鐘完成。Cloud Build 會 cache，第二次以後快很多。

## 改密碼

```bash
# 用新密碼蓋過去（產一個新 version）
echo -n 'NewPassword' | gcloud secrets versions add shopee-dashboard-auth-pass --data-file=-

# 重新部署讓新 version 生效
bash deploy.sh
```

## 看現在的密碼

```bash
gcloud secrets versions access latest --secret=shopee-dashboard-auth-pass
```

---

## 變數設定參考

可以 override 的環境變數：

| 變數 | 預設 | 說明 |
|---|---|---|
| `PROJECT_ID` | `shopee-dashboard-{username}` | 你的 GCP project ID（全 GCP 唯一） |
| `REGION` | `asia-east1` | 台灣最近的 region |
| `SERVICE` | `shopee-dashboard` | Cloud Run service 名稱 |
| `BUCKET` | `${PROJECT_ID}-data` | GCS bucket 名稱 |
| `BASIC_AUTH_USER` | `admin` | 登入帳號 |
| `BASIC_AUTH_PASS` | **必填** | 登入密碼 |
| `ANTHROPIC_API_KEY` | （無）| AI 顧問用，沒設走 rule_based fallback |
| `SENDGRID_API_KEY` | （無）| 寄月報用，沒設走 mock |
| `SENDGRID_FROM_EMAIL` | （無）| 寄信來源（要先在 SendGrid 驗證） |
| `SHOPEE_PARTNER_ID` | （無）| 蝦皮 Partner API |
| `SHOPEE_PARTNER_KEY` | （無）| 蝦皮 Partner API |

---

## 預期費用（每月）

| 項目 | 用量假設 | 費用 |
|---|---|---|
| Cloud Run | 1 instance × 24h × 30天，但只算實際請求時間 | $0（免費額度 2M req/月） |
| Cloud Storage | 100MB 資料 + 1000 次操作 | $0（免費額度） |
| Cloud Build | 每月 10 次部署 | $0（每天 120 build min 免費） |
| Egress | 給你瀏覽器看 | < $1（亞洲免費 1GB/月） |
| **總計** | 個人用 | **$0-2 / 月** |

代操業者規模化（10+ 客戶頻繁用）後，可能升到 $5-10/月。

---

## 常見問題

**Q: 如何看 Cloud Run logs？**
```bash
gcloud run logs tail shopee-dashboard --region=asia-east1
```

**Q: 如何更新某個資料（例如改 customers.json）？**
```bash
# 從本機推上去
gsutil cp customers.json gs://${PROJECT_ID}-data/

# 或下載 GCS 上的版本
gsutil cp gs://${PROJECT_ID}-data/ad_reports.json ./
```

**Q: 想砍掉重練？**
```bash
gcloud run services delete shopee-dashboard --region=asia-east1
gsutil rm -r gs://${PROJECT_ID}-data
# 想連 project 都砍：
gcloud projects delete ${PROJECT_ID}
```

**Q: 怎麼回來改成本機跑？**
不用改任何 code — 直接 `python server.py` 即可（DATA_DIR 沒設就用本機）。

---

## 升級到方案 B/C（給客戶用）

當你想：
- 加自定 domain（`https://dashboard.yourdomain.com`）
- 加 Google OAuth 登入（取代 Basic Auth）
- 多客戶角色權限
- Cloud SQL 取代 JSON 檔

→ 跟 Claude 說「升級到方案 B」會給你下一階段的 plan。
