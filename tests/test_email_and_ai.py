"""email_client + ai_advisor 單元測試（純 mock 模式，不打外部 API）"""
import pytest

import email_client
import ai_advisor


class TestEmailClient:
    def test_no_api_key_is_unconfigured(self, monkeypatch):
        monkeypatch.delenv("SENDGRID_API_KEY", raising=False)
        monkeypatch.delenv("SENDGRID_FROM_EMAIL", raising=False)
        assert email_client.is_configured() is False
        status = email_client.config_status()
        assert status["mode"] == "mock"
        assert status["ready"] is False

    def test_send_returns_mock_when_unconfigured(self, monkeypatch):
        monkeypatch.delenv("SENDGRID_API_KEY", raising=False)
        result = email_client.send_monthly_report(
            to_email="x@y.com", subject="test", report_data={})
        assert result["status"] == "mock"
        assert result["message_id"] is None
        assert "未設定" in result.get("note", "")

    def test_render_html_basic(self):
        html = email_client.render_monthly_report_html({
            "customer": {"name": "測試客戶", "shop_ids": ["S001"],
                         "contract_start": "2026-01-01", "contract_end": "2026-12-31"},
            "month": "2026-05",
            "metrics": {"revenue": 1000, "spend": 250, "roas": 4.0,
                        "target_roas": 3.5, "kpi_achieved": True},
            "highlights": ["亮點1"], "next_month_plan": ["計畫1"],
        })
        assert "測試客戶" in html
        assert "2026-05" in html
        assert "NT$1,000" in html or "NT$ 1,000" in html or "1,000" in html
        assert "✓ 達標" in html or "✓" in html

    def test_render_html_with_profit(self):
        html = email_client.render_monthly_report_html({
            "customer": {"name": "X", "shop_ids": []},
            "month": "2026-05",
            "metrics": {"revenue": 1000, "spend": 250, "roas": 4.0,
                        "target_roas": 3.5, "kpi_achieved": True},
            "profit": {"revenue": 1000, "ad_spend": 250, "cogs": 400,
                       "shipping_total": 60, "platform_fee": 55,
                       "payment_fee": 30, "net_profit": 205},
            "highlights": [], "next_month_plan": [],
        })
        assert "真實獲利" in html
        assert "205" in html


class TestAiAdvisor:
    def test_no_api_key_is_rule_based(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        assert ai_advisor.is_configured() is False
        assert ai_advisor.config_status()["mode"] == "rule_based"

    def test_rule_based_negative_profit(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        result = ai_advisor.generate_insights({
            "kpi": {"spend": 1000, "revenue": 800, "roas": 0.8, "target_roas": 3.5},
            "profit": {"net_profit": -500, "revenue": 800, "ad_spend": 1000},
            "top_products": [],
            "triggered_rules": [],
        })
        assert result["_meta"]["source"] == "rule_based"
        # 應該偵測到淨利為負
        assert any("虧損" in d or "負" in d for d in result["diagnosis"])

    def test_rule_based_with_loss_product(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        result = ai_advisor.generate_insights({
            "kpi": {"spend": 100, "revenue": 50, "roas": 0.5, "target_roas": 3.0},
            "profit": None,
            "top_products": [
                {"product_name": "差商品", "roas": 0.5, "spend": 100, "revenue": 50},
            ],
            "triggered_rules": [],
        })
        # 應該建議檢查/降出價
        joined = " ".join(result["immediate_actions"])
        assert "差商品" in joined or "降" in joined or "暫停" in joined

    def test_rule_based_returns_required_keys(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        result = ai_advisor.generate_insights({"kpi": {}, "profit": None,
                                                "top_products": [], "triggered_rules": []})
        assert "diagnosis" in result
        assert "immediate_actions" in result
        assert "this_week_plan" in result
        assert isinstance(result["diagnosis"], list)
