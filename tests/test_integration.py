"""端對端整合測試 — 用 FastAPI TestClient 跑 server 的主要 endpoint。

策略：
- monkeypatch DATA_DIR → 用 tmp_path 隔離，不污染本機資料
- 啟動前先塞一份合理 fixture data（客戶 + 上傳）
- 每個 endpoint 至少驗證：
  - HTTP 200
  - response 結構（有 data 鍵）
  - source/_meta 是 real（如果該 endpoint 支援）
"""
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def isolated_server(tmp_path, monkeypatch):
    """每個 test 獨立 DATA_DIR + 預先塞 fixture data"""
    # 隔離 DATA_DIR
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    # 不要啟用 auth（測試 endpoint 不要被 401 擋）
    monkeypatch.delenv("BASIC_AUTH_USER", raising=False)
    monkeypatch.delenv("BASIC_AUTH_PASS", raising=False)
    # 不要啟動 csv_watcher 背景 task（測試環境會卡）
    monkeypatch.setenv("DISABLE_WATCHER", "1")

    # 預塞客戶
    customers = [{
        "id": "CU0", "name": "測試客戶", "shop_ids": ["test_shop"],
        "contract_start": "2026-01-01", "contract_end": "2026-12-31",
        "monthly_fee": 30000, "target_roas": 3.5, "target_acos": 28,
        "settle_day": 25, "status": "active", "email": "test@example.com",
    }]
    (tmp_path / "customers.json").write_text(
        json.dumps(customers, ensure_ascii=False), encoding="utf-8")

    # 預塞 ad_reports
    today = date.today()
    upload_id = "UP-test-001"
    rows = [{
        "upload_id": upload_id, "shop": "test_shop",
        "date": (today - timedelta(days=i)).isoformat(),
        "product_id": f"P{i+1}", "product_name": f"商品{i+1}",
        "spend": 100 * (i+1), "revenue": 500 * (i+1),
        "orders": 5 * (i+1), "clicks": 30 * (i+1), "impressions": 1000 * (i+1),
    } for i in range(3)]
    store = {
        "uploads": [{
            "id": upload_id, "shop": "test_shop", "filename": "test.csv",
            "uploaded_at": today.isoformat() + "T00:00:00Z",
            "encoding": "utf-8-sig", "raw_columns": [], "column_map": {},
            "skipped_rows": 0,
            "summary": {
                "row_count": 3, "products": 3,
                "total_spend": 600, "total_revenue": 3000,
                "report_period_start": (today - timedelta(days=2)).isoformat(),
                "report_period_end": today.isoformat(),
                "period_days": 3, "is_lifetime_report": False,
            },
        }],
        "rows": rows,
    }
    (tmp_path / "ad_reports.json").write_text(
        json.dumps(store, ensure_ascii=False), encoding="utf-8")

    # 預塞 profit_config
    profit_cfg = {"P1": {
        "cost": 200, "shipping": 60, "shipping_rate": 0,
        "platform_fee_rate": 0.055, "payment_fee_rate": 0.03,
        "selling_price": 500,
    }}
    (tmp_path / "profit_config.json").write_text(
        json.dumps(profit_cfg, ensure_ascii=False), encoding="utf-8")

    # 重載所有 module（讓 paths.DATA_DIR 重新解析）
    for mod in ("paths", "ad_data_store", "csv_watcher", "google_trends_client",
                "shopee_public_api", "ai_advisor", "shopee_client",
                "email_client", "rules_engine", "ad_report_parser",
                "auth", "server"):
        sys.modules.pop(mod, None)

    import server
    client = TestClient(server.app)
    return client


class TestSimpleEndpoints:
    """純 GET，不需要 body / 路徑參數"""

    @pytest.mark.parametrize("path", [
        "/healthz",
        "/api/v1/ad-data/status",
        "/api/v1/uploads",
        "/api/v1/customers",
        "/api/v1/accounts",
        "/api/v1/profit/config",
        "/api/v1/profit/summary?period=month",
        "/api/v1/products/health-scores",
        "/api/v1/products/rising",
        "/api/v1/products/optimized",
        "/api/v1/budget/pacing",
        "/api/v1/budget/alerts",
        "/api/v1/events",
        "/api/v1/suggestions",
        "/api/v1/snapshots",
        "/api/v1/rules",
        "/api/v1/audiences",
        "/api/v1/team",
        "/api/v1/notify/config",
        "/api/v1/heatmap",
        "/api/v1/competitor/keywords",
        "/api/v1/competitor/new-products",
        "/api/v1/competitor/price-drops",
        "/api/v1/competitors/list",
        "/api/v1/keywords/groups",
        "/api/v1/keywords/seasonal-calendar",
        "/api/v1/adjustments",
        "/api/v1/actions",
        "/api/v1/kpi?period=week",
        "/api/v1/kpi/trend?days=7",
        "/api/v1/auth/shopee/connections",
        "/api/v1/shopee/status",
        "/api/v1/email/status",
        "/api/v1/ai/status",
        "/api/v1/external/status",
        "/api/v1/watcher/status",
        "/api/v1/ad-data/products?period=month",
        "/api/v1/keywords/performance?period=month",
        "/api/v1/placements/performance?period=month",
    ])
    def test_endpoint_returns_200(self, isolated_server, path):
        r = isolated_server.get(path)
        assert r.status_code == 200, f"{path} returned {r.status_code}: {r.text[:200]}"
        # /healthz 例外（沒包 data 鍵）
        if "healthz" not in path:
            body = r.json()
            assert "data" in body or "error" in body, f"{path} missing data/error key"


class TestRealDataInResponses:
    """有 CSV 資料時，特定 endpoint 應該回 source=real"""

    def test_kpi_returns_real(self, isolated_server):
        r = isolated_server.get("/api/v1/kpi?period=month")
        assert r.status_code == 200
        d = r.json()["data"]
        assert d["_meta"]["source"] == "real"
        assert d["spend"] > 0  # 不是 mock 的 285420 之類
        assert d["revenue"] > 0

    def test_profit_returns_real(self, isolated_server):
        r = isolated_server.get("/api/v1/profit/summary?period=month")
        assert r.status_code == 200
        d = r.json()["data"]
        assert d["_meta"]["source"] == "real"

    def test_accounts_lists_real_shops(self, isolated_server):
        r = isolated_server.get("/api/v1/accounts")
        assert r.status_code == 200
        d = r.json()["data"]
        assert d["_meta"]["source"] == "real"
        assert d["total"] >= 1
        # 應該有 test_shop（fixture 塞的）
        shop_ids = [x["id"] for x in d["items"]]
        assert "test_shop" in shop_ids

    def test_ad_data_status_has_real(self, isolated_server):
        r = isolated_server.get("/api/v1/ad-data/status")
        assert r.status_code == 200
        d = r.json()["data"]
        assert d["has_real_data"] is True
        assert d["upload_count"] >= 1
        assert "test_shop" in d["shops"]


class TestPostEndpoints:
    """主要 POST endpoint"""

    def test_rules_preview(self, isolated_server):
        # 先建一條規則
        rule = {"name": "test", "metric": "roas", "operator": "lt",
                "threshold": 100, "window_days": 7, "action": "pause",
                "enabled": True, "scope": "all"}
        r = isolated_server.post("/api/v1/rules", json=rule)
        assert r.status_code == 200
        # 跑 preview
        r = isolated_server.post("/api/v1/rules/preview")
        assert r.status_code == 200
        d = r.json()["data"]
        assert "results" in d

    def test_ai_insights_returns_structure(self, isolated_server):
        r = isolated_server.post("/api/v1/ai/insights",
                                 json={"customer_id": "CU0", "period": "month"})
        assert r.status_code == 200
        d = r.json()["data"]
        assert "diagnosis" in d
        assert "immediate_actions" in d
        assert "this_week_plan" in d


class TestStaticAssets:
    """前端靜態資源"""

    def test_root_returns_html(self, isolated_server):
        r = isolated_server.get("/")
        assert r.status_code == 200
        assert "<html" in r.text.lower() or "<!doctype" in r.text.lower()

    def test_api_js(self, isolated_server):
        r = isolated_server.get("/api.js")
        assert r.status_code == 200
        assert "javascript" in r.headers.get("content-type", "").lower()
        assert "BASE = '/api/v1'" in r.text or "request" in r.text
