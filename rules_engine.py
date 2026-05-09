"""
自動規則引擎
─────────────────────────
責任：把 rules.json 的條件對 ad_data_store 中的真實商品做評估，
     回傳「此刻會觸發哪些規則 + 影響哪些商品」。

對外公開函式：
    evaluate_rules(rules, shop=None) → list[TriggerResult]
    apply_rules(rules, dry_run=True, shop=None) → ApplyReport

支援的 metric:  roas / ctr / cpc / spend / acos / orders
支援的 operator: lt / gt / lte / gte
支援的 action:   pause / lower_bid / raise_bid / notify
                （目前 mock 執行，只紀錄；未來接 Shopee Ads API 時實作真實寫入）
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta, timezone
from typing import Any

import ad_data_store

OPERATORS = {
    "lt":  lambda a, b: a < b,
    "lte": lambda a, b: a <= b,
    "gt":  lambda a, b: a > b,
    "gte": lambda a, b: a >= b,
}


@dataclass
class TriggerResult:
    rule_id: str
    rule_name: str
    metric: str
    operator: str
    threshold: float
    action: str
    action_value: float | None
    matched_products: list[dict] = field(default_factory=list)
    evaluated_count: int = 0
    triggered_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class ApplyReport:
    triggered: list[TriggerResult]
    dry_run: bool
    ran_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    total_actions: int = 0


# ─────────────────────────── 計算 product metric 值 ───────────────────────────

def _product_metric(product: dict, metric: str) -> float | None:
    """從聚合後的商品資料取對應指標。注意：百分比類（ctr/acos）回傳「百分比值」（如 2.5 表 2.5%）。"""
    if metric == "roas":
        return product.get("roas")
    if metric == "ctr":
        # aggregate_products 已乘以 100
        return product.get("ctr")
    if metric == "cpc":
        return product.get("cpc")
    if metric == "spend":
        return product.get("spend")
    if metric == "revenue":
        return product.get("revenue")
    if metric == "acos":
        return product.get("acos")
    if metric == "orders":
        return product.get("orders")
    if metric == "clicks":
        return product.get("clicks")
    return None


def _eval_one_rule(rule: dict, products: list[dict]) -> TriggerResult:
    metric = rule.get("metric", "")
    op = rule.get("operator", "")
    thr = rule.get("threshold", 0)
    fn = OPERATORS.get(op)
    matched = []
    if fn:
        for p in products:
            v = _product_metric(p, metric)
            if v is None:
                continue
            try:
                if fn(v, thr):
                    matched.append({
                        "product_id": p.get("product_id"),
                        "product_name": p.get("product_name"),
                        "shop": p.get("shop"),
                        "value": round(v, 3) if isinstance(v, float) else v,
                        "metric": metric,
                        "spend_30d": p.get("spend"),
                        "revenue_30d": p.get("revenue"),
                    })
            except (TypeError, ValueError):
                continue
    return TriggerResult(
        rule_id=rule.get("id", ""),
        rule_name=rule.get("name", "(unnamed)"),
        metric=metric, operator=op, threshold=thr,
        action=rule.get("action", "notify"),
        action_value=rule.get("action_value"),
        matched_products=matched,
        evaluated_count=len(products),
    )


# ─────────────────────────── 主 API ───────────────────────────

def evaluate_rules(rules: list[dict], shop: str | None = None,
                   period_days: int = 7) -> list[TriggerResult]:
    """評估所有 enabled 規則 → 回傳每條規則的命中結果（dry-run 用）"""
    products = ad_data_store.aggregate_products(
        shop=shop,
        period="week" if period_days <= 7 else "month",
        limit=500,
    )
    results = []
    for rule in rules:
        if not rule.get("enabled", True):
            continue
        # window_days 規則內定義的範圍，目前簡化為 period_days
        results.append(_eval_one_rule(rule, products))
    return results


def apply_rules(rules: list[dict], dry_run: bool = True,
                shop: str | None = None) -> ApplyReport:
    """執行所有 enabled 規則 → 觸發紀錄 + 動作（目前 dry_run 才安全）"""
    triggers = evaluate_rules(rules, shop=shop)
    triggers_with_match = [t for t in triggers if t.matched_products]
    total_actions = sum(len(t.matched_products) for t in triggers_with_match)

    if not dry_run:
        # 真實執行：未來接 Shopee Ads API 時在這裡寫入「降出價/暫停」
        # 目前只紀錄到 rules 的 last_triggered / trigger_count
        now = datetime.now(timezone.utc).isoformat()
        for t in triggers_with_match:
            for r in rules:
                if r.get("id") == t.rule_id:
                    r["last_triggered"] = now
                    r["trigger_count"] = r.get("trigger_count", 0) + len(t.matched_products)
                    break

    return ApplyReport(
        triggered=triggers_with_match,
        dry_run=dry_run,
        total_actions=total_actions,
    )
