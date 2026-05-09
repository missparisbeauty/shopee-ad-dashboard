# 蝦皮廣告代操儀表板

> FastAPI + GCP Cloud Run · 整合 CSV 解析、AI 顧問、Shopee/SendGrid/Claude API
>
> **版本**：v3.1 (cloud-ready) · **測試**：123 個 pytest 全綠

代操業者用的多客戶 dashboard。每天匯出蝦皮廣告 CSV → 拖到資料夾 → 自動解析+顯示真實獲利、AI 建議、規則式優化。

---

## 主要功能

| 區塊 | 重點 |
|---|---|
| **KPI / 真實獲利** | spend / revenue / ROAS + 扣完成本/運費/平台抽成/刷卡費的真實淨利 |
| **商品健康度** | 評分 + 庫存 + 銷量 + 評論綜合健康度（CSV 商品為主） |
| **預算燒爆預警** | 7 日均花費 × 1.2 當預算，預測幾點燒完 |
| **自動規則引擎** | `metric (roas/ctr/cpc/spend) operator threshold action`，dry-run + real run |
| **AI 顧問建議** | 真實 CSV 數據 → Claude API 產出診斷 / 立即動作 / 本週計畫（沒 key 用 rule-based fallback） |
| **客戶 CRM + 月報** | 月底產月報 PDF + SendGrid 真實寄送 |
| **資料夾自動上傳** | 拖 CSV 到 watch/{shop}/ → 5 秒自動解析（支援 GDrive/OneDrive 同步路徑） |
| **多客戶切換器** | 全域 sticky 下拉，所有頁面同步 filter |
| **Google Trends** | 今日台灣熱搜（Google Trends RSS） |

---

## 架構

```
你的瀏覽器
    ↓ HTTPS (Cloud Run 自動)
    ↓ HTTP Basic Auth (Secret Manager 管密碼)
    ↓
Cloud Run service: shopee-dashboard
    ├─ container 跑 FastAPI（吃 $PORT, $DATA_DIR）
    ├─ min/max instance = 1（避免 JSON 檔併發寫衝突）
    └─ mount GCS bucket → /data（持久化所有 JSON 資料）
```

**為什麼 min=max=1？**
JSON 當資料庫 + GCS volume mount → 多 instance 同時寫會壞檔。1 instance 處理上百並發 request 沒問題（個人/小團隊用）。

---

## 快速啟動（本機）

```bash
git clone https://github.com/missparisbeauty/shopee-ad-dashboard.git
cd shopee-ad-dashboard

pip install -r requirements.txt
python server.py    # 啟動於 http://localhost:8765
```

---

## 部署到 GCP Cloud Run

完整指南：[`README-DEPLOY.md`](./README-DEPLOY.md)

簡版：
```bash
gcloud auth login
bash setup-gcp.sh   # 第一次：建 bucket / secrets / API
bash deploy.sh      # 部署
```

---

## 環境變數（全部選用）

| 變數 | 說明 |
|---|---|
| `PORT` | Cloud Run 必設（自動） |
| `DATA_DIR` | 資料目錄（cloud 設 `/data`，本機不設） |
| `BASIC_AUTH_USER` / `BASIC_AUTH_PASS` | 登入帳密（沒設不啟用 auth） |
| `ANTHROPIC_API_KEY` | AI 顧問用 Claude（沒設用 rule-based fallback） |
| `SENDGRID_API_KEY` / `SENDGRID_FROM_EMAIL` | 月報真實寄送 |
| `SHOPEE_PARTNER_ID` / `SHOPEE_PARTNER_KEY` | 蝦皮 Open API（補商品 rating/stock） |

---

## 測試

```bash
python -m pytest tests/ -v
# 123 passed in ~24s
```

涵蓋：
- 解析器（中英欄位 / UTF-8 BOM / BIG5 / 蝦皮 lifetime 摘要列）
- 聚合（KPI / profit / products / multi-shop filter）
- 自動規則引擎
- Auth middleware
- 37 個 GET endpoint 整合測試
- Edge cases（fee_rate normalize / 重複上傳偵測 / lifetime fallback date）

---

## 蝦皮 Open API 限制

| 蝦皮 Open API 提供 | 蝦皮**不**提供 |
|---|---|
| ✅ 商品列表 / 評分 / 庫存 | ❌ **廣告花費 / ROAS / CTR / 點擊** |
| ✅ 訂單明細 | ❌ 廣告報表 |

**廣告數據只能靠 CSV 上傳**（蝦皮策略性封閉）。本系統用「混合策略」：廣告數據 → CSV 自動上傳；商品/訂單 → Shopee Open API stub。

---

## License

私人專案，未公開授權。如需引用程式碼，請聯絡 [missparisbeauty](https://github.com/missparisbeauty)。

---

🤖 Built with [Claude Code](https://claude.com/claude-code)
