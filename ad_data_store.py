"""
廣告報表資料儲存層
─────────────────────────
責任：把 parser 產出的正規化 rows 持久化到 ad_reports.json，並提供聚合查詢
     讓 KPI / products / profit 等頁面可以讀到真實數據。

對外公開函式：
    save_upload(upload_id, shop, filename, parse_result)
    list_uploads()
    delete_upload(upload_id)
    aggregate_kpi(shop=None, period="week")
    aggregate_products(shop=None, period="week", limit=50)
    aggregate_daily_trend(shop=None, days=7)
    has_real_data() → bool
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Iterable

from paths import DATA_DIR
ROOT = DATA_DIR  # 向後相容（本機 = Path(__file__).parent，cloud = $DATA_DIR）
STORE_FILE = ROOT / "ad_reports.json"
_lock = Lock()


def _empty_store() -> dict[str, Any]:
    return {"uploads": [], "rows": []}


def _load() -> dict[str, Any]:
    if not STORE_FILE.exists():
        return _empty_store()
    try:
        return json.loads(STORE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # 壞檔不要讓整個 server 死掉，但保留壞檔備份方便排查
        backup = STORE_FILE.with_suffix(".json.broken")
        try:
            STORE_FILE.rename(backup)
        except OSError:
            pass
        return _empty_store()


def _save(store: dict[str, Any]) -> None:
    tmp = STORE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STORE_FILE)


# ─────────────────────────── 寫入 ───────────────────────────

def save_upload(upload_id: str, shop: str, filename: str, parse_result) -> dict[str, Any]:
    """parse_result 為 ad_report_parser.ParseResult"""
    upload_meta = {
        "id": upload_id,
        "shop": shop,
        "filename": filename,
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
        "encoding": parse_result.encoding,
        "raw_columns": parse_result.raw_columns,
        "column_map": parse_result.column_map,
        "skipped_rows": parse_result.skipped_rows,
        "summary": parse_result.summary,
    }
    rows_to_store = []
    for r in parse_result.rows:
        rows_to_store.append({
            "upload_id": upload_id,
            "shop": shop,
            **r,
        })
    with _lock:
        store = _load()
        store["uploads"].append(upload_meta)
        store["rows"].extend(rows_to_store)
        _save(store)
    return upload_meta


def delete_upload(upload_id: str) -> bool:
    with _lock:
        store = _load()
        before = len(store["uploads"])
        store["uploads"] = [u for u in store["uploads"] if u["id"] != upload_id]
        store["rows"] = [r for r in store["rows"] if r.get("upload_id") != upload_id]
        if len(store["uploads"]) == before:
            return False
        _save(store)
        return True


# ─────────────────────────── 讀取 ───────────────────────────

def list_uploads() -> list[dict[str, Any]]:
    store = _load()
    return sorted(store["uploads"], key=lambda u: u["uploaded_at"], reverse=True)


def has_real_data() -> bool:
    return bool(_load()["rows"])


def _filter(rows: Iterable[dict], shop: str | None, since: date | None) -> list[dict]:
    out = []
    for r in rows:
        if shop and r.get("shop") != shop:
            continue
        if since:
            d = r.get("date")
            if not d:
                continue
            try:
                if date.fromisoformat(d) < since:
                    continue
            except ValueError:
                continue
        out.append(r)
    return out


_PERIOD_DAYS = {"yesterday": 1, "week": 7, "month": 30}


def _period_to_since(period: str) -> date | None:
    days = _PERIOD_DAYS.get(period)
    if days is None:
        return None
    return date.today() - timedelta(days=days)


# ─────────────────────────── 聚合 ───────────────────────────

def aggregate_kpi(shop: str | None = None, period: str = "week") -> dict[str, Any]:
    """聚合 KPI（spend/revenue/roas/acos/ctr/cpm），同時計算 vs 前一期的 delta。"""
    rows = _load()["rows"]
    since = _period_to_since(period)
    cur = _filter(rows, shop, since)
    cur_sum = _sum_metrics(cur)

    # 前一期：同樣天數，往前推
    days = _PERIOD_DAYS.get(period, 7)
    prev_since = (since - timedelta(days=days)) if since else None
    prev_until = since
    prev = [r for r in _filter(rows, shop, prev_since)
            if not prev_until or (r.get("date") and date.fromisoformat(r["date"]) < prev_until)]
    prev_sum = _sum_metrics(prev)

    def pct_delta(cur_v: float, prev_v: float) -> int:
        if not prev_v:
            return 0
        return round((cur_v - prev_v) / prev_v * 100)

    return {
        "spend": int(round(cur_sum["spend"])),
        "revenue": int(round(cur_sum["revenue"])),
        "roas": round(cur_sum["roas"], 2),
        "acos": round(cur_sum["acos"] * 100, 1),
        "ctr": round(cur_sum["ctr"] * 100, 2),
        "cpm": int(round(cur_sum["cpm"])),
        "spend_delta": pct_delta(cur_sum["spend"], prev_sum["spend"]),
        "revenue_delta": pct_delta(cur_sum["revenue"], prev_sum["revenue"]),
        "target_roas": 3.5,
        "target_acos": 28,
        "_meta": {
            "period": period,
            "shop": shop,
            "row_count": len(cur),
            "prev_row_count": len(prev),
            "source": "real",
        },
    }


def _sum_metrics(rows: list[dict]) -> dict[str, float]:
    spend = sum(r.get("spend") or 0 for r in rows)
    revenue = sum(r.get("revenue") or 0 for r in rows)
    clicks = sum(r.get("clicks") or 0 for r in rows)
    impr = sum(r.get("impressions") or 0 for r in rows)
    orders = sum(r.get("orders") or 0 for r in rows)
    return {
        "spend": spend,
        "revenue": revenue,
        "clicks": clicks,
        "impressions": impr,
        "orders": orders,
        "roas": (revenue / spend) if spend else 0,
        "ctr": (clicks / impr) if impr else 0,
        "acos": (spend / revenue) if revenue else 0,
        "cpm": (spend / impr * 1000) if impr else 0,
    }


def aggregate_products(shop: str | None = None, period: str = "month", limit: int = 50) -> list[dict]:
    """以 product_id 聚合，回傳排序好的商品表現清單。"""
    rows = _filter(_load()["rows"], shop, _period_to_since(period))
    bucket: dict[str, dict] = {}
    for r in rows:
        pid = r.get("product_id") or r.get("product_name")
        # 過濾掉「-」「N/A」這種蝦皮報表的 placeholder
        if not pid or pid in ("-", "—", "N/A", "n/a", "null"):
            continue
        b = bucket.setdefault(pid, {
            "product_id": pid,
            "product_name": r.get("product_name") or pid,
            "spend": 0.0, "revenue": 0.0,
            "clicks": 0, "impressions": 0, "orders": 0,
        })
        b["spend"] += r.get("spend") or 0
        b["revenue"] += r.get("revenue") or 0
        b["clicks"] += r.get("clicks") or 0
        b["impressions"] += r.get("impressions") or 0
        b["orders"] += r.get("orders") or 0

    out = []
    for b in bucket.values():
        b["roas"] = round(b["revenue"] / b["spend"], 2) if b["spend"] else 0
        b["ctr"] = round(b["clicks"] / b["impressions"] * 100, 2) if b["impressions"] else 0
        b["cpc"] = round(b["spend"] / b["clicks"], 2) if b["clicks"] else 0
        b["acos"] = round(b["spend"] / b["revenue"] * 100, 1) if b["revenue"] else 0
        b["spend"] = round(b["spend"], 2)
        b["revenue"] = round(b["revenue"], 2)
        out.append(b)

    out.sort(key=lambda x: x["revenue"], reverse=True)
    return out[:limit]


def aggregate_daily_trend(shop: str | None = None, days: int = 7) -> list[dict]:
    """每日 spend / revenue 趨勢（給折線圖）。"""
    since = date.today() - timedelta(days=days)
    rows = _filter(_load()["rows"], shop, since)
    daily: dict[str, dict] = {}
    for r in rows:
        d = r.get("date")
        if not d:
            continue
        b = daily.setdefault(d, {"date": d, "spend": 0.0, "revenue": 0.0})
        b["spend"] += r.get("spend") or 0
        b["revenue"] += r.get("revenue") or 0
    points = sorted(daily.values(), key=lambda x: x["date"])
    for p in points:
        p["spend"] = int(round(p["spend"]))
        p["revenue"] = int(round(p["revenue"]))
    return points


def aggregate_keywords(shop: str | None = None, period: str = "month",
                       limit: int = 100) -> list[dict]:
    """以「關鍵字」分組聚合（需 keyword/版位 CSV 才有資料）。
    回傳排序好的關鍵字表現清單。
    """
    rows = _filter(_load()["rows"], shop, _period_to_since(period))
    bucket: dict[str, dict] = {}
    for r in rows:
        kw = r.get("keyword")
        if not kw or kw in ("-", "—", "N/A", "n/a", "null"):
            continue
        b = bucket.setdefault(kw, {
            "keyword": kw,
            "spend": 0.0, "revenue": 0.0,
            "clicks": 0, "impressions": 0, "orders": 0,
        })
        b["spend"] += r.get("spend") or 0
        b["revenue"] += r.get("revenue") or 0
        b["clicks"] += r.get("clicks") or 0
        b["impressions"] += r.get("impressions") or 0
        b["orders"] += r.get("orders") or 0

    out = []
    for b in bucket.values():
        b["roas"] = round(b["revenue"] / b["spend"], 2) if b["spend"] else 0
        b["ctr"] = round(b["clicks"] / b["impressions"] * 100, 2) if b["impressions"] else 0
        b["cpc"] = round(b["spend"] / b["clicks"], 2) if b["clicks"] else 0
        b["acos"] = round(b["spend"] / b["revenue"] * 100, 1) if b["revenue"] else 0
        b["spend"] = round(b["spend"], 2)
        b["revenue"] = round(b["revenue"], 2)
        out.append(b)
    out.sort(key=lambda x: x["revenue"], reverse=True)
    return out[:limit]


def aggregate_placements(shop: str | None = None, period: str = "month",
                          limit: int = 100) -> list[dict]:
    """以「版位」分組聚合（需關鍵字/版位 CSV 才有資料）。"""
    rows = _filter(_load()["rows"], shop, _period_to_since(period))
    bucket: dict[str, dict] = {}
    for r in rows:
        pl = r.get("placement")
        if not pl or pl in ("-", "—", "N/A", "n/a", "null"):
            continue
        b = bucket.setdefault(pl, {
            "placement": pl,
            "spend": 0.0, "revenue": 0.0,
            "clicks": 0, "impressions": 0, "orders": 0,
        })
        b["spend"] += r.get("spend") or 0
        b["revenue"] += r.get("revenue") or 0
        b["clicks"] += r.get("clicks") or 0
        b["impressions"] += r.get("impressions") or 0
        b["orders"] += r.get("orders") or 0

    out = []
    for b in bucket.values():
        b["roas"] = round(b["revenue"] / b["spend"], 2) if b["spend"] else 0
        b["ctr"] = round(b["clicks"] / b["impressions"] * 100, 2) if b["impressions"] else 0
        b["cpc"] = round(b["spend"] / b["clicks"], 2) if b["clicks"] else 0
        b["acos"] = round(b["spend"] / b["revenue"] * 100, 1) if b["revenue"] else 0
        b["spend"] = round(b["spend"], 2)
        b["revenue"] = round(b["revenue"], 2)
        out.append(b)
    out.sort(key=lambda x: x["revenue"], reverse=True)
    return out[:limit]


def aggregate_weekday_roas(shop: str | None = None, days: int = 60) -> dict[str, Any]:
    """依星期幾聚合 ROAS。蝦皮廣告 CSV 只有「日」粒度、沒有「幾點」，
    所以最細只能做到星期，無法做 7×24 分時熱力圖。
    """
    since = date.today() - timedelta(days=days)
    rows = _filter(_load()["rows"], shop, since)
    buckets = {i: {"spend": 0.0, "revenue": 0.0, "dates": set()} for i in range(7)}
    for r in rows:
        d = r.get("date")
        if not d:
            continue
        try:
            wd = date.fromisoformat(d).weekday()  # 0=週一 ... 6=週日
        except ValueError:
            continue
        b = buckets[wd]
        b["spend"] += r.get("spend") or 0
        b["revenue"] += r.get("revenue") or 0
        b["dates"].add(d)
    labels = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]
    cells = []
    for i in range(7):
        b = buckets[i]
        cells.append({
            "day": labels[i],
            "roas": round(b["revenue"] / b["spend"], 2) if b["spend"] else 0.0,
            "spend": int(round(b["spend"])),
            "revenue": int(round(b["revenue"])),
            "days_count": len(b["dates"]),
        })
    return {"cells": cells, "window_days": days}


def list_shops_with_data() -> list[str]:
    return sorted({r["shop"] for r in _load()["rows"] if r.get("shop")})


def find_duplicate_upload(shop: str, filename: str,
                          period_start: str | None,
                          period_end: str | None) -> dict | None:
    """檢查 store 內是否已有「同 shop + 同檔名 + 同期間」的上傳。
    回傳第一筆重複（如有），用來警告 user 避免重複加總。
    """
    for u in _load()["uploads"]:
        if u["shop"] != shop:
            continue
        if u["filename"] != filename:
            continue
        s = u.get("summary") or {}
        if s.get("report_period_start") == period_start and s.get("report_period_end") == period_end:
            return u
    return None


# ─────────────────────────── 進階聚合（給 profit / health / budget 用） ───────────────────────────

# 預設成本參數（沒有對應 product_id 的設定時 fallback）
DEFAULT_COST_RATIO = 0.42      # 商品成本 = 營收 * 0.42
DEFAULT_SHIPPING_RATIO = 0.06  # 運費 = 營收 * 0.06
DEFAULT_PLATFORM_FEE_RATE = 0.055
DEFAULT_PAYMENT_FEE_RATE = 0.03


def aggregate_profit(
    shop: str | None = None,
    period: str = "month",
    profit_config: dict | None = None,
) -> dict[str, Any]:
    """真實獲利：營收 - 廣告 - 成本 - 運費 - 平台抽成 - 刷卡費

    profit_config: dict[product_id, {cost, shipping, platform_fee_rate, payment_fee_rate, selling_price}]
    沒對應到的 product_id 用預設比例。
    """
    profit_config = profit_config or {}
    rows = _filter(_load()["rows"], shop, _period_to_since(period))

    total_revenue = 0.0
    total_ad_spend = 0.0
    total_cogs = 0.0
    total_shipping = 0.0
    total_platform_fee = 0.0
    total_payment_fee = 0.0
    matched_pids = 0
    total_pids = 0
    seen_pids = set()

    for r in rows:
        rev = r.get("revenue") or 0
        spend = r.get("spend") or 0
        orders = r.get("orders") or 0
        pid = r.get("product_id")
        total_revenue += rev
        total_ad_spend += spend

        cfg = profit_config.get(pid) if pid else None
        if pid and pid not in seen_pids:
            seen_pids.add(pid)
            total_pids += 1
            if cfg:
                matched_pids += 1

        if cfg:
            # rate > 1 視為「user 輸入百分比」(5 = 5%) 自動 / 100
            # rate <= 1 視為小數 (0.05 = 5%)
            def _normalize_rate(v):
                if v is None or v == 0:
                    return 0
                return v / 100 if v > 1 else v

            # 用實際成本：cost × orders
            total_cogs += (cfg.get("cost") or 0) * orders
            # 運費：優先看 shipping_rate (% of revenue)，>0 用比例；否則用 shipping × orders
            ship_rate = _normalize_rate(cfg.get("shipping_rate") or 0)
            if ship_rate > 0:
                total_shipping += rev * ship_rate
            else:
                total_shipping += (cfg.get("shipping") or 0) * orders
            pf_rate = _normalize_rate(cfg.get("platform_fee_rate")) or DEFAULT_PLATFORM_FEE_RATE
            pay_rate = _normalize_rate(cfg.get("payment_fee_rate")) or DEFAULT_PAYMENT_FEE_RATE
            total_platform_fee += rev * pf_rate
            total_payment_fee += rev * pay_rate
        else:
            total_cogs += rev * DEFAULT_COST_RATIO
            total_shipping += rev * DEFAULT_SHIPPING_RATIO
            total_platform_fee += rev * DEFAULT_PLATFORM_FEE_RATE
            total_payment_fee += rev * DEFAULT_PAYMENT_FEE_RATE

    net_profit = total_revenue - total_ad_spend - total_cogs - total_shipping - total_platform_fee - total_payment_fee

    return {
        "period": period,
        "revenue": int(round(total_revenue)),
        "ad_spend": int(round(total_ad_spend)),
        "cogs": int(round(total_cogs)),
        "shipping_total": int(round(total_shipping)),
        "platform_fee": int(round(total_platform_fee)),
        "payment_fee": int(round(total_payment_fee)),
        "net_profit": int(round(net_profit)),
        "real_roi": round(net_profit / total_ad_spend, 2) if total_ad_spend else 0,
        "ad_profit_margin": round(net_profit / total_revenue * 100, 1) if total_revenue else 0,
        "gross_margin": round((total_revenue - total_cogs) / total_revenue * 100, 1) if total_revenue else 0,
        "_meta": {
            "source": "real",
            "shop": shop,
            "row_count": len(rows),
            "products": total_pids,
            "products_with_cost_config": matched_pids,
        },
    }


def aggregate_today_spend(shop: str | None = None) -> dict[str, Any]:
    """今日 vs 昨日 vs 7 日均花費（給預算燃燒燒爆預測）。"""
    today = date.today()
    yesterday = today - timedelta(days=1)
    week_since = today - timedelta(days=7)

    rows = _filter(_load()["rows"], shop, week_since)
    today_spend = 0.0
    yesterday_spend = 0.0
    week_spend = 0.0
    days_with_data = set()
    for r in rows:
        d = r.get("date")
        spend = r.get("spend") or 0
        if not d:
            continue
        try:
            dt = date.fromisoformat(d)
        except ValueError:
            continue
        days_with_data.add(d)
        week_spend += spend
        if dt == today:
            today_spend += spend
        if dt == yesterday:
            yesterday_spend += spend

    avg_daily = week_spend / max(len(days_with_data), 1)
    return {
        "today_spend": round(today_spend, 2),
        "yesterday_spend": round(yesterday_spend, 2),
        "week_spend": round(week_spend, 2),
        "avg_daily_spend": round(avg_daily, 2),
        "days_with_data": len(days_with_data),
    }


def aggregate_budget_pacing(
    shop: str | None = None,
    accounts: list[dict] | None = None,
    daily_budget_per_shop: dict[str, float] | None = None,
) -> list[dict]:
    """每個帳號的日花費 vs 預算。
    accounts: 從 server.get_accounts() 拿到的列表，當 fallback 顯示用
    daily_budget_per_shop: 顯式指定每個帳號的日預算；沒給則用該帳號 7 日均花費 × 1.2 當預算
    """
    daily_budget_per_shop = daily_budget_per_shop or {}
    rows = _load()["rows"]
    today = date.today()
    yesterday = today - timedelta(days=1)
    week_since = today - timedelta(days=7)

    # 先依 shop 分組
    shops_in_data = sorted({r["shop"] for r in rows if r.get("shop")})
    if shop:
        shops_in_data = [shop] if shop in shops_in_data else []

    items = []
    now_hour = datetime.now(timezone.utc).hour or 1  # 防 0
    for sh in shops_in_data:
        sh_rows = [r for r in rows if r.get("shop") == sh]
        # 7 日花費
        recent = [r for r in sh_rows if r.get("date") and date.fromisoformat(r["date"]) >= week_since]
        week_spend = sum(r.get("spend") or 0 for r in recent)
        days_with_data = len({r["date"] for r in recent if r.get("date")})
        avg_daily = week_spend / max(days_with_data, 1) if days_with_data else 0

        today_spend = sum(r.get("spend") or 0 for r in sh_rows
                          if r.get("date") == today.isoformat())
        fallback_day_str = None
        if today_spend == 0:
            # 找該店家「最近一天有資料」的花費當代表
            shop_dates = sorted({r["date"] for r in sh_rows if r.get("date")}, reverse=True)
            if shop_dates:
                fallback_day_str = shop_dates[0]
                today_spend = sum(r.get("spend") or 0 for r in sh_rows
                                  if r.get("date") == fallback_day_str)
        using_fallback_day = fallback_day_str is not None

        daily_budget = daily_budget_per_shop.get(sh) or (avg_daily * 1.2)
        burn_rate = today_spend / daily_budget if daily_budget else 0
        if burn_rate > 0 and not using_fallback_day:
            depleted = min(24, now_hour / burn_rate)
        else:
            depleted = 24

        if burn_rate > 1.2: status = "danger"
        elif burn_rate > 0.9 and now_hour < 18: status = "warn"
        else: status = "ok"

        # 從 accounts 找對應 shop_name（若有）
        shop_name = sh
        if accounts:
            for a in accounts:
                if a.get("id") == sh or a.get("name") == sh:
                    shop_name = a.get("name") or sh
                    break

        items.append({
            "shop_id": sh,
            "shop_name": shop_name,
            "daily_budget": round(daily_budget),
            "burn_today": round(today_spend),
            "burn_rate_pct": round(burn_rate * 100),
            "predicted_depletion_hour": round(depleted, 1),
            "status": status,
            "_meta": {
                "days_with_data": days_with_data,
                "fallback_day": using_fallback_day,
                "fallback_day_date": fallback_day_str,
            },
        })
    return items


def aggregate_product_sales(shop: str | None = None, days: int = 30) -> dict[str, dict]:
    """近 N 天每個商品的 spend/revenue/orders/clicks/impressions（給商品健康度用）。
    對於 date=None 的 lifetime 統計列，一律包含（總體報表沒有按日切的情況）。
    回傳 dict[product_id, metrics]
    """
    since = date.today() - timedelta(days=days)
    rows = []
    for r in _load()["rows"]:
        if shop and r.get("shop") != shop:
            continue
        d = r.get("date")
        if not d:
            rows.append(r)  # lifetime / 總體統計列：無條件包含
            continue
        try:
            if date.fromisoformat(d) >= since:
                rows.append(r)
        except ValueError:
            continue
    bucket: dict[str, dict] = {}
    for r in rows:
        pid = r.get("product_id")
        if not pid or pid in ("-", "—", "N/A", "n/a", "null"):
            continue
        b = bucket.setdefault(pid, {
            "product_id": pid,
            "product_name": r.get("product_name") or pid,
            "shop": r.get("shop"),
            "spend": 0.0, "revenue": 0.0,
            "clicks": 0, "impressions": 0, "orders": 0, "units_sold": 0,
        })
        b["spend"] += r.get("spend") or 0
        b["revenue"] += r.get("revenue") or 0
        b["clicks"] += r.get("clicks") or 0
        b["impressions"] += r.get("impressions") or 0
        b["orders"] += r.get("orders") or 0
        b["units_sold"] += r.get("units_sold") or 0
    return bucket
