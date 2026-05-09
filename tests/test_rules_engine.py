"""rules_engine 單元測試"""
from datetime import date, timedelta
import pytest

import rules_engine
import ad_data_store


@pytest.fixture
def store_with_data(tmp_path, monkeypatch):
    """建一份 fake store 給 rules_engine 評估"""
    test_file = tmp_path / "test_ad_reports.json"
    monkeypatch.setattr(ad_data_store, "STORE_FILE", test_file)
    today = date.today().isoformat()

    class R:
        pass
    r = R()
    r.encoding = "utf-8-sig"
    r.raw_columns = []
    r.column_map = {}
    r.skipped_rows = 0
    r.summary = {}
    r.rows = [
        # 高 ROAS 商品
        {"date": today, "product_id": "P_GOOD", "product_name": "好商品",
         "spend": 100, "revenue": 1000, "orders": 5, "clicks": 50, "impressions": 1000},
        # 低 ROAS 商品
        {"date": today, "product_id": "P_BAD", "product_name": "差商品",
         "spend": 200, "revenue": 100, "orders": 1, "clicks": 30, "impressions": 5000},
        # 中等
        {"date": today, "product_id": "P_MID", "product_name": "中商品",
         "spend": 150, "revenue": 450, "orders": 3, "clicks": 40, "impressions": 2000},
    ]
    ad_data_store.save_upload("UP-T", "S001", "test.csv", r)
    return ad_data_store


class TestEvaluateRules:
    def test_roas_lt_threshold(self, store_with_data):
        rule = {"id": "R1", "name": "低 ROAS 暫停", "metric": "roas",
                "operator": "lt", "threshold": 1.5,
                "action": "pause", "enabled": True}
        results = rules_engine.evaluate_rules([rule])
        assert len(results) == 1
        # P_BAD ROAS = 100/200 = 0.5 → 命中
        # P_MID ROAS = 450/150 = 3.0 → 不中
        # P_GOOD ROAS = 1000/100 = 10 → 不中
        assert len(results[0].matched_products) == 1
        assert results[0].matched_products[0]["product_id"] == "P_BAD"

    def test_roas_gt_threshold(self, store_with_data):
        rule = {"id": "R2", "name": "高 ROAS 加碼", "metric": "roas",
                "operator": "gt", "threshold": 5,
                "action": "raise_bid", "enabled": True}
        results = rules_engine.evaluate_rules([rule])
        # 只有 P_GOOD 命中
        assert len(results[0].matched_products) == 1
        assert results[0].matched_products[0]["product_id"] == "P_GOOD"

    def test_disabled_rule_skipped(self, store_with_data):
        rule = {"id": "R3", "metric": "roas", "operator": "lt", "threshold": 100,
                "action": "pause", "enabled": False}
        results = rules_engine.evaluate_rules([rule])
        assert results == []

    def test_multiple_rules(self, store_with_data):
        rules = [
            {"id": "R1", "metric": "roas", "operator": "lt", "threshold": 1.5,
             "action": "pause", "enabled": True},
            {"id": "R2", "metric": "spend", "operator": "gt", "threshold": 150,
             "action": "lower_bid", "enabled": True},
        ]
        results = rules_engine.evaluate_rules(rules)
        assert len(results) == 2

    def test_invalid_operator_returns_empty(self, store_with_data):
        rule = {"id": "RX", "metric": "roas", "operator": "INVALID",
                "threshold": 1, "action": "pause", "enabled": True}
        results = rules_engine.evaluate_rules([rule])
        assert results[0].matched_products == []


class TestApplyRules:
    def test_dry_run_does_not_update_count(self, store_with_data):
        rule = {"id": "R1", "metric": "roas", "operator": "lt", "threshold": 1.5,
                "action": "pause", "enabled": True}
        report = rules_engine.apply_rules([rule], dry_run=True)
        assert report.dry_run is True
        # rule 中不應有 trigger_count
        assert "trigger_count" not in rule

    def test_real_run_updates_count(self, store_with_data):
        rule = {"id": "R1", "name": "test", "metric": "roas", "operator": "lt",
                "threshold": 1.5, "action": "pause", "enabled": True}
        report = rules_engine.apply_rules([rule], dry_run=False)
        assert report.dry_run is False
        assert rule.get("trigger_count", 0) == 1  # P_BAD 命中
        assert rule.get("last_triggered") is not None

    def test_total_actions_count(self, store_with_data):
        # 寬條件命中 3 個
        rule = {"id": "R1", "metric": "spend", "operator": "gte",
                "threshold": 100, "action": "notify", "enabled": True}
        report = rules_engine.apply_rules([rule], dry_run=True)
        assert report.total_actions == 3
