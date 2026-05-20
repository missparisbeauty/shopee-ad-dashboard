"""Edge case 測試 — 涵蓋之前修過的 bug 跟容易誤用的情境

每個 bug 都鎖住，避免 regression。
"""
import json
from datetime import date, timedelta

import pytest


# ─────────────────────────── fee_rate normalize ───────────────────────────

class TestFeeRateNormalize:
    """user 經常輸入「5」想表達 5%，但內部用小數 0.05。
    aggregate_profit 應該 auto-normalize：> 1 視為百分比。
    """

    @pytest.fixture
    def store_with_one_product(self, tmp_path, monkeypatch):
        import ad_data_store
        monkeypatch.setattr(ad_data_store, "STORE_FILE", tmp_path / "ad.json")
        today = date.today().isoformat()

        class R:
            encoding = "utf-8"
            raw_columns = []
            column_map = {}
            skipped_rows = 0
            summary = {}
            rows = [{
                "date": today, "product_id": "P1", "product_name": "test",
                "spend": 100, "revenue": 1000, "orders": 10,
            }]
        ad_data_store.save_upload("UP-1", "S1", "test.csv", R())
        return ad_data_store

    def test_decimal_rate_kept_as_is(self, store_with_one_product):
        """0.055 (5.5%) 應視為小數，不變"""
        cfg = {"P1": {"cost": 0, "shipping": 0, "shipping_rate": 0,
                      "platform_fee_rate": 0.055, "payment_fee_rate": 0.03}}
        p = store_with_one_product.aggregate_profit(period="week", profit_config=cfg)
        # platform_fee = 1000 * 0.055 = 55
        assert p["platform_fee"] == 55

    def test_integer_rate_normalized(self, store_with_one_product):
        """5.5 (大於 1) 應視為「百分比」，自動 / 100"""
        cfg = {"P1": {"cost": 0, "shipping": 0, "shipping_rate": 0,
                      "platform_fee_rate": 5.5, "payment_fee_rate": 3.0}}
        p = store_with_one_product.aggregate_profit(period="week", profit_config=cfg)
        # platform_fee = 1000 * 5.5/100 = 55（同上）
        assert p["platform_fee"] == 55
        # payment_fee = 1000 * 3/100 = 30
        assert p["payment_fee"] == 30

    def test_extreme_value_15_means_15percent(self, store_with_one_product):
        """user 輸入 15 → 15% （之前 bug：直接當 1500% 算造成淨利爆負）"""
        cfg = {"P1": {"cost": 0, "shipping": 0, "shipping_rate": 5,
                      "platform_fee_rate": 15, "payment_fee_rate": 3}}
        p = store_with_one_product.aggregate_profit(period="week", profit_config=cfg)
        # shipping = 1000 * 5/100 = 50（不是 5000）
        assert p["shipping_total"] == 50
        # platform = 1000 * 15/100 = 150（不是 15000）
        assert p["platform_fee"] == 150

    def test_zero_shipping_rate_uses_per_order(self, store_with_one_product):
        """shipping_rate = 0 → 用 shipping × orders"""
        cfg = {"P1": {"cost": 0, "shipping": 60, "shipping_rate": 0,
                      "platform_fee_rate": 0.055, "payment_fee_rate": 0.03}}
        p = store_with_one_product.aggregate_profit(period="week", profit_config=cfg)
        # shipping = 60 * 10 orders = 600
        assert p["shipping_total"] == 600

    def test_shipping_rate_overrides_per_order(self, store_with_one_product):
        """shipping_rate > 0 應蓋掉 shipping per-order"""
        cfg = {"P1": {"cost": 0, "shipping": 60, "shipping_rate": 5,
                      "platform_fee_rate": 0.055, "payment_fee_rate": 0.03}}
        p = store_with_one_product.aggregate_profit(period="week", profit_config=cfg)
        # 用 % 不是每單：1000 * 5% = 50（不是 60 * 10 = 600）
        assert p["shipping_total"] == 50


# ─────────────────────────── 重複上傳偵測 ───────────────────────────

class TestDuplicateUploadDetection:
    """find_duplicate_upload 應該偵測「同 shop + 同檔名 + 同期間」"""

    @pytest.fixture
    def store(self, tmp_path, monkeypatch):
        import ad_data_store
        monkeypatch.setattr(ad_data_store, "STORE_FILE", tmp_path / "ad.json")

        class R:
            encoding = "utf-8"
            raw_columns = []
            column_map = {}
            skipped_rows = 0
            rows = [{"date": "2026-04-01", "product_id": "P1",
                     "spend": 100, "revenue": 500}]
            summary = {
                "report_period_start": "2026-01-01",
                "report_period_end": "2026-04-01",
            }
        ad_data_store.save_upload("UP-existing", "S001", "report.csv", R())
        return ad_data_store

    def test_finds_exact_duplicate(self, store):
        dup = store.find_duplicate_upload(
            shop="S001", filename="report.csv",
            period_start="2026-01-01", period_end="2026-04-01",
        )
        assert dup is not None
        assert dup["id"] == "UP-existing"

    def test_different_shop_not_duplicate(self, store):
        dup = store.find_duplicate_upload(
            shop="S002", filename="report.csv",
            period_start="2026-01-01", period_end="2026-04-01",
        )
        assert dup is None

    def test_different_filename_not_duplicate(self, store):
        dup = store.find_duplicate_upload(
            shop="S001", filename="other.csv",
            period_start="2026-01-01", period_end="2026-04-01",
        )
        assert dup is None

    def test_different_period_not_duplicate(self, store):
        dup = store.find_duplicate_upload(
            shop="S001", filename="report.csv",
            period_start="2026-04-01", period_end="2026-05-01",
        )
        assert dup is None

    def test_no_period_not_duplicate(self, store):
        """期間 None → 不算重複（lifetime 報表 fallback 邏輯）"""
        dup = store.find_duplicate_upload(
            shop="S001", filename="report.csv",
            period_start=None, period_end=None,
        )
        assert dup is None


# ─────────────────────────── multi-shop filter ───────────────────────────

class TestMultiShopFilter:
    """單一 customer 可能綁多個 shop（CU0 早期綁 S001 + 自家3C旗艦店）"""

    @pytest.fixture
    def store_multi_shop(self, tmp_path, monkeypatch):
        import ad_data_store
        monkeypatch.setattr(ad_data_store, "STORE_FILE", tmp_path / "ad.json")
        today = date.today().isoformat()

        class R:
            encoding = "utf-8"
            raw_columns = []
            column_map = {}
            skipped_rows = 0
            summary = {}
            rows = []

        # Shop A
        r1 = R()
        r1.rows = [{"date": today, "product_id": "P1",
                    "spend": 100, "revenue": 500, "orders": 5}]
        ad_data_store.save_upload("UP-A", "shopA", "a.csv", r1)
        # Shop B
        r2 = R()
        r2.rows = [{"date": today, "product_id": "P2",
                    "spend": 200, "revenue": 800, "orders": 8}]
        ad_data_store.save_upload("UP-B", "shopB", "b.csv", r2)
        # Shop A 第二筆
        r3 = R()
        r3.rows = [{"date": today, "product_id": "P3",
                    "spend": 50, "revenue": 300, "orders": 3}]
        ad_data_store.save_upload("UP-A2", "shopA", "a2.csv", r3)
        return ad_data_store

    def test_aggregate_kpi_no_filter_sums_all(self, store_multi_shop):
        kpi = store_multi_shop.aggregate_kpi(period="week")
        assert kpi["spend"] == 350  # 100 + 200 + 50
        assert kpi["revenue"] == 1600  # 500 + 800 + 300
        assert kpi["_meta"]["row_count"] == 3

    def test_aggregate_kpi_shopA_only(self, store_multi_shop):
        kpi = store_multi_shop.aggregate_kpi(shop="shopA", period="week")
        assert kpi["spend"] == 150  # 100 + 50
        assert kpi["revenue"] == 800  # 500 + 300
        assert kpi["_meta"]["row_count"] == 2

    def test_aggregate_kpi_shopB_only(self, store_multi_shop):
        kpi = store_multi_shop.aggregate_kpi(shop="shopB", period="week")
        assert kpi["spend"] == 200
        assert kpi["revenue"] == 800
        assert kpi["_meta"]["row_count"] == 1

    def test_aggregate_kpi_unknown_shop_zero(self, store_multi_shop):
        kpi = store_multi_shop.aggregate_kpi(shop="nonexistent", period="week")
        assert kpi["spend"] == 0
        assert kpi["revenue"] == 0
        assert kpi["_meta"]["row_count"] == 0

    def test_list_shops_with_data(self, store_multi_shop):
        shops = store_multi_shop.list_shops_with_data()
        assert sorted(shops) == ["shopA", "shopB"]

    def test_aggregate_products_shop_filter(self, store_multi_shop):
        prods_a = store_multi_shop.aggregate_products(shop="shopA", period="week")
        prod_ids = [p["product_id"] for p in prods_a]
        assert "P1" in prod_ids
        assert "P3" in prod_ids
        assert "P2" not in prod_ids


# ─────────────────────────── lifetime fallback date ───────────────────────────

class TestLifetimeFallbackDate:
    """蝦皮 lifetime CSV 沒「日期」欄位，parser 從「期間」摘要列抽 end_date"""

    def test_parser_uses_period_end_when_date_missing(self):
        import ad_report_parser as p
        csv = (
            "所有CPC成效報告\n"
            "期間,2026/04/01 - 2026/05/03\n"
            "\n"
            "商品 ID,廣告名稱,曝光數,點擊數,花費,銷售金額\n"
            "PROD1,廣告A,1000,30,500,2000\n"
        )
        r = p.parse_csv(csv.encode("utf-8-sig"))
        assert len(r.rows) == 1
        # row 沒「日期」欄，但 metadata 抽到「期間」 → fallback 用 end_date
        assert r.rows[0]["date"] == "2026-05-03"
        assert r.summary["is_lifetime_report"] is True
        assert r.summary["period_days"] == 33

    def test_single_day_period_not_lifetime(self):
        import ad_report_parser as p
        csv = (
            "所有CPC成效報告\n"
            "期間,2026/05/03 - 2026/05/03\n"
            "\n"
            "商品 ID,廣告名稱,曝光數,點擊數,花費,銷售金額\n"
            "PROD1,廣告A,1000,30,500,2000\n"
        )
        r = p.parse_csv(csv.encode("utf-8-sig"))
        assert r.summary["is_lifetime_report"] is False
        assert r.summary["period_days"] == 1


# ─────────────────────────── 報表類型偵測 ───────────────────────────

class TestReportTypeDetection:
    """parser 應該能區分「總體廣告」vs「關鍵字/版位」CSV"""

    def test_overall_report_no_keyword_column(self):
        """純總體報表 → report_type == 'overall'"""
        import ad_report_parser as p
        csv = (
            "期間,2026/05/03 - 2026/05/03\n"
            "\n"
            "商品 ID,廣告名稱,曝光數,點擊數,花費,銷售金額\n"
            "PROD1,廣告A,1000,30,500,2000\n"
        )
        r = p.parse_csv(csv.encode("utf-8-sig"))
        assert r.summary["report_type"] == "overall"

    def test_keyword_report_with_keyword_column(self):
        """有「關鍵字」欄 → report_type == 'keyword_placement'"""
        import ad_report_parser as p
        csv = (
            "期間,2026/05/03 - 2026/05/03\n"
            "\n"
            "關鍵字,曝光數,點擊數,花費,銷售金額\n"
            "保濕乳液,1000,30,500,2000\n"
            "美白精華,800,25,400,1500\n"
        )
        r = p.parse_csv(csv.encode("utf-8-sig"))
        assert r.summary["report_type"] == "keyword_placement"
        assert r.rows[0]["keyword"] == "保濕乳液"
        assert r.rows[1]["keyword"] == "美白精華"

    def test_placement_report_with_placement_column(self):
        """有「版位」欄 → report_type == 'keyword_placement'"""
        import ad_report_parser as p
        csv = (
            "期間,2026/05/03 - 2026/05/03\n"
            "\n"
            "版位,曝光數,點擊數,花費,銷售金額\n"
            "搜尋結果頁,5000,150,800,3500\n"
            "分類頁推薦,3000,80,400,1800\n"
        )
        r = p.parse_csv(csv.encode("utf-8-sig"))
        assert r.summary["report_type"] == "keyword_placement"
        assert r.rows[0]["placement"] == "搜尋結果頁"

    def test_mixed_keyword_and_placement(self):
        """同時有 keyword 跟 placement → keyword_placement"""
        import ad_report_parser as p
        csv = (
            "期間,2026/05/03 - 2026/05/03\n"
            "\n"
            "關鍵字,版位,曝光數,點擊數,花費,銷售金額\n"
            "保濕乳液,搜尋頁,1000,30,500,2000\n"
        )
        r = p.parse_csv(csv.encode("utf-8-sig"))
        assert r.summary["report_type"] == "keyword_placement"
        assert r.rows[0]["keyword"] == "保濕乳液"
        assert r.rows[0]["placement"] == "搜尋頁"

    def test_english_keyword_alias(self):
        """英文 'Keyword' 也要認"""
        import ad_report_parser as p
        csv = (
            "Date,Keyword,Impressions,Clicks,Cost,Sales\n"
            "2026/05/03,moisturizer,1000,30,500,2000\n"
        )
        r = p.parse_csv(csv.encode("utf-8-sig"))
        assert r.summary["report_type"] == "keyword_placement"
        assert r.rows[0]["keyword"] == "moisturizer"

    def test_keyword_only_row_kept_no_product(self):
        """只有 keyword 沒 product_id 的 row 也要保留（不被 skip）"""
        import ad_report_parser as p
        csv = (
            "關鍵字,曝光數,點擊數,花費,銷售金額\n"
            "保濕乳液,1000,30,500,2000\n"
        )
        r = p.parse_csv(csv.encode("utf-8-sig"))
        assert len(r.rows) == 1
        assert r.rows[0]["keyword"] == "保濕乳液"
        # 沒 product_id 但有 keyword 應該不算被 skip
        assert r.skipped_rows == 0


class TestDetectReportTypeFunction:
    """直接測試 detect_report_type() helper"""

    def test_empty_returns_overall(self):
        import ad_report_parser as p
        assert p.detect_report_type({}, []) == "overall"

    def test_has_keyword_in_column_map_and_rows(self):
        import ad_report_parser as p
        column_map = {"關鍵字": "keyword", "花費": "spend"}
        rows = [{"keyword": "test", "spend": 100}]
        assert p.detect_report_type(column_map, rows) == "keyword_placement"

    def test_mapped_but_all_rows_empty_keyword_returns_overall(self):
        """欄位有對到但實際 rows 都沒填 keyword → 還是 overall（fallback）"""
        import ad_report_parser as p
        column_map = {"關鍵字": "keyword"}
        rows = [{"spend": 100}, {"revenue": 200}]
        assert p.detect_report_type(column_map, rows) == "overall"
