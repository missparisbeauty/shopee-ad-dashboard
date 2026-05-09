"""ad_data_store 單元測試
用 monkeypatch 隔離 STORE_FILE 路徑，避免污染本機真實資料。
"""
import json
from datetime import date, timedelta
from pathlib import Path

import pytest


@pytest.fixture
def store(tmp_path, monkeypatch):
    """每個 test 用獨立 tmp store"""
    import ad_data_store
    test_file = tmp_path / "test_ad_reports.json"
    monkeypatch.setattr(ad_data_store, "STORE_FILE", test_file)
    return ad_data_store


def _make_result(rows, summary=None, encoding="utf-8-sig"):
    """組一個 fake ParseResult"""
    class R:
        pass
    r = R()
    r.rows = rows
    r.encoding = encoding
    r.raw_columns = []
    r.column_map = {}
    r.skipped_rows = 0
    r.summary = summary or {"row_count": len(rows)}
    return r


def _today_iso(offset_days=0):
    return (date.today() + timedelta(days=offset_days)).isoformat()


class TestSaveAndLoad:
    def test_save_creates_upload_meta(self, store):
        result = _make_result([
            {"date": _today_iso(-1), "product_id": "P1", "product_name": "A",
             "spend": 100, "revenue": 500, "orders": 5, "clicks": 30, "impressions": 1000},
        ])
        meta = store.save_upload("UP-1", "S001", "test.csv", result)
        assert meta["id"] == "UP-1"
        assert meta["shop"] == "S001"
        uploads = store.list_uploads()
        assert len(uploads) == 1
        assert uploads[0]["id"] == "UP-1"

    def test_delete_upload(self, store):
        result = _make_result([{"date": _today_iso(-1), "product_id": "P1",
                                "spend": 100, "revenue": 500}])
        store.save_upload("UP-A", "S001", "a.csv", result)
        store.save_upload("UP-B", "S002", "b.csv", result)
        assert store.delete_upload("UP-A") is True
        assert store.delete_upload("UP-A") is False  # 已不存在
        assert len(store.list_uploads()) == 1

    def test_has_real_data(self, store):
        assert store.has_real_data() is False
        result = _make_result([{"date": _today_iso(-1), "product_id": "P1",
                                "spend": 1, "revenue": 1}])
        store.save_upload("UP-1", "S001", "a.csv", result)
        assert store.has_real_data() is True


class TestAggregateKpi:
    def test_basic_aggregation(self, store):
        rows = [
            {"date": _today_iso(-1), "product_id": "P1",
             "spend": 100, "revenue": 500, "orders": 5,
             "clicks": 30, "impressions": 1000},
            {"date": _today_iso(-2), "product_id": "P2",
             "spend": 200, "revenue": 800, "orders": 8,
             "clicks": 60, "impressions": 2000},
        ]
        store.save_upload("UP-1", "S001", "a.csv", _make_result(rows))
        kpi = store.aggregate_kpi(period="week")
        assert kpi["spend"] == 300
        assert kpi["revenue"] == 1300
        assert kpi["roas"] == pytest.approx(1300/300, rel=1e-2)
        assert kpi["_meta"]["source"] == "real"
        assert kpi["_meta"]["row_count"] == 2

    def test_shop_filter(self, store):
        rows_a = [{"date": _today_iso(-1), "product_id": "P1", "spend": 100, "revenue": 500}]
        rows_b = [{"date": _today_iso(-1), "product_id": "P1", "spend": 200, "revenue": 800}]
        store.save_upload("UP-A", "S001", "a.csv", _make_result(rows_a))
        store.save_upload("UP-B", "S002", "b.csv", _make_result(rows_b))
        kpi_a = store.aggregate_kpi(shop="S001", period="week")
        kpi_b = store.aggregate_kpi(shop="S002", period="week")
        kpi_all = store.aggregate_kpi(period="week")
        assert kpi_a["spend"] == 100
        assert kpi_b["spend"] == 200
        assert kpi_all["spend"] == 300


class TestAggregateProfit:
    def test_uses_profit_config(self, store):
        rows = [{"date": _today_iso(-1), "product_id": "P1",
                 "spend": 100, "revenue": 1000, "orders": 5}]
        store.save_upload("UP-1", "S001", "a.csv", _make_result(rows))
        cfg = {"P1": {"cost": 100, "shipping": 50,
                      "platform_fee_rate": 0.055, "payment_fee_rate": 0.03}}
        p = store.aggregate_profit(period="week", profit_config=cfg)
        # cogs = 100 * 5 = 500
        assert p["cogs"] == 500
        # shipping = 50 * 5 = 250
        assert p["shipping_total"] == 250
        # platform = 1000 * 0.055 = 55
        assert p["platform_fee"] == 55
        # payment = 1000 * 0.03 = 30
        assert p["payment_fee"] == 30
        # net = 1000 - 100 - 500 - 250 - 55 - 30 = 65
        assert p["net_profit"] == 65
        assert p["_meta"]["products_with_cost_config"] == 1

    def test_fallback_to_default_ratio(self, store):
        rows = [{"date": _today_iso(-1), "product_id": "PUNKNOWN",
                 "spend": 100, "revenue": 1000, "orders": 5}]
        store.save_upload("UP-1", "S001", "a.csv", _make_result(rows))
        p = store.aggregate_profit(period="week", profit_config={})
        # cogs = 1000 * 0.42 = 420
        assert p["cogs"] == 420
        assert p["_meta"]["products_with_cost_config"] == 0


class TestAggregateProducts:
    def test_filters_invalid_pid(self, store):
        rows = [
            {"date": _today_iso(-1), "product_id": "P1", "spend": 100, "revenue": 500},
            {"date": _today_iso(-1), "product_id": "-", "spend": 50, "revenue": 200},  # 應過濾
            {"date": _today_iso(-1), "product_id": "N/A", "spend": 30, "revenue": 100},  # 應過濾
        ]
        store.save_upload("UP-1", "S001", "a.csv", _make_result(rows))
        prods = store.aggregate_products(period="week")
        assert len(prods) == 1
        assert prods[0]["product_id"] == "P1"


class TestLifetimeFallback:
    def test_aggregate_product_sales_includes_dateless(self, store):
        """date=None 的 lifetime 列應該被 product_sales 聚合包含"""
        rows = [
            {"date": None, "product_id": "P1", "product_name": "Lifetime",
             "spend": 100, "revenue": 500, "orders": 5,
             "units_sold": 5, "clicks": 30, "impressions": 1000},
            {"date": _today_iso(-1), "product_id": "P2", "product_name": "Daily",
             "spend": 200, "revenue": 800, "orders": 8,
             "units_sold": 8, "clicks": 60, "impressions": 2000},
        ]
        store.save_upload("UP-1", "S001", "a.csv", _make_result(rows))
        sales = store.aggregate_product_sales(days=30)
        assert "P1" in sales  # date=None 也要包含
        assert "P2" in sales
