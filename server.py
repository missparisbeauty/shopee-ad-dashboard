"""
蝦皮代操儀表板 — Mock API Server
單檔 FastAPI 服務，所有端點回傳結構化 JSON 模仿真實蝦皮 Open API。

啟動：
    pip install -r requirements.txt
    python server.py
    # 然後開 http://localhost:8000

未來接真實蝦皮 Open API：
    - 各 endpoint 內把 mock 資料換成 httpx 呼叫 partner.shopeemobile.com
    - 加入 HMAC-SHA256 簽章（partner_id + partner_key + shop_id + timestamp）
    - 處理 access_token / refresh_token 流程
"""
import csv
import hashlib
import hmac
import io
import json
import os
import random
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import ad_report_parser
import ad_data_store
import csv_watcher
import email_client
import shopee_client
import shopee_public_api
import google_trends_client
import rules_engine
import ai_advisor

# 資料路徑用 paths.DATA_DIR 統一管理（cloud-aware）
# 但靜態檔案（demo.html / api.js）仍從 __file__ parent 讀
from paths import DATA_DIR
ROOT = DATA_DIR  # 所有 *.json / uploads / watch 都放這
STATIC_ROOT = Path(__file__).parent  # demo.html / api.js
app = FastAPI(title="蝦皮代操儀表板 API", version="3.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Basic Auth middleware（有設 BASIC_AUTH_USER+PASS 環境變數才啟用）
import auth as _auth
app.middleware("http")(_auth.basic_auth_middleware)


@app.get("/healthz")
def healthz():
    """Cloud Run / load balancer 健康檢查（不經過 auth）"""
    return {"status": "ok"}


@app.on_event("startup")
async def _on_startup() -> None:
    """伺服器啟動時，自動開啟 CSV 資料夾監看（可在 UI 或 API 停用）"""
    csv_watcher.start(scan_interval=5)


@app.on_event("shutdown")
async def _on_shutdown() -> None:
    csv_watcher.stop()

# ─────────────────────────── 工具：統一回應格式 ───────────────────────────

def ok(data):
    return {"data": data, "error": None, "ts": datetime.now(timezone.utc).isoformat()}


# ─────────────────────────── KPI ───────────────────────────

@app.get("/api/v1/kpi")
def get_kpi(
    period: str = Query("week", pattern="^(yesterday|week|month)$"),
    source: str = Query("auto", pattern="^(auto|mock|real)$"),
    shop: str | None = None,
):
    """KPI 摘要。
    - source=auto：有上傳真實 CSV 時用真實，否則 mock（預設）
    - source=real：強制用 CSV 解析後的資料，沒資料時回 mock + 標記
    - source=mock：強制 mock
    """
    presets = {
        "yesterday": {"spend": 46920, "revenue": 196800, "roas": 4.19, "acos": 23.8, "ctr": 2.5, "cpm": 49,
                       "spend_delta": +8, "revenue_delta": +12, "target_roas": 3.5, "target_acos": 28,
                       "_meta": {"source": "mock", "period": "yesterday"}},
        "week":      {"spend": 285420, "revenue": 1142800, "roas": 4.00, "acos": 25.0, "ctr": 2.4, "cpm": 48,
                       "spend_delta": +12, "revenue_delta": +18, "target_roas": 3.5, "target_acos": 28,
                       "_meta": {"source": "mock", "period": "week"}},
        "month":     {"spend": 1180600, "revenue": 4520000, "roas": 3.83, "acos": 26.1, "ctr": 2.3, "cpm": 51,
                       "spend_delta": +6, "revenue_delta": +9, "target_roas": 3.5, "target_acos": 28,
                       "_meta": {"source": "mock", "period": "month"}},
    }
    use_real = source == "real" or (source == "auto" and ad_data_store.has_real_data())
    if use_real and ad_data_store.has_real_data():
        return ok(ad_data_store.aggregate_kpi(shop=shop, period=period))
    return ok(presets[period])


@app.get("/api/v1/kpi/trend")
def get_trend(
    days: int = Query(7, ge=1, le=90),
    source: str = Query("auto", pattern="^(auto|mock|real)$"),
    shop: str | None = None,
):
    use_real = source == "real" or (source == "auto" and ad_data_store.has_real_data())
    if use_real and ad_data_store.has_real_data():
        points = ad_data_store.aggregate_daily_trend(shop=shop, days=days)
        if points:
            return ok({"points": points, "_meta": {"source": "real"}})

    today = datetime.now(timezone.utc).date()
    base_rev, base_spend = 148000, 38000
    points = []
    for i in range(days):
        d = today - timedelta(days=days - 1 - i)
        growth = 1 + (i / days) * 0.35
        noise = random.uniform(0.92, 1.08)
        points.append({
            "date": d.isoformat(),
            "revenue": int(base_rev * growth * noise),
            "spend": int(base_spend * growth * noise),
        })
    return ok({"points": points, "_meta": {"source": "mock"}})


# ─────────────────────────── 帳號總表 ───────────────────────────

@app.get("/api/v1/accounts")
def get_accounts():
    """帳號總表 — 每個有 CSV 資料的 shop 一筆。
    從 ad_data_store 算近 30 天的 spend/revenue/roas，shop_name 對到 customers.json。
    沒任何 CSV 時回空 array（不再回 mock）。
    """
    if not ad_data_store.has_real_data():
        return ok({"items": [], "total": 0,
                   "_meta": {"source": "real",
                             "note": "尚未上傳 CSV，請到「整合與設定」上傳廣告報表"}})

    # shop_id → customer 對照
    customers = _load_customers()
    shop_to_cust = {}
    for c in customers:
        for sid in c.get("shop_ids", []):
            shop_to_cust[sid] = c

    items = []
    for shop in ad_data_store.list_shops_with_data():
        kpi = ad_data_store.aggregate_kpi(shop=shop, period="month")
        cust = shop_to_cust.get(shop)
        target_roas = (cust or {}).get("target_roas", 3.5)
        roas = kpi.get("roas", 0) or 0
        if roas == 0:
            status = "no_data"
        elif roas < target_roas * 0.7:
            status = "danger"
        elif roas < target_roas:
            status = "warn"
        else:
            status = "active"
        items.append({
            "id": shop,
            "type": "client" if cust else "own",  # 已綁客戶 = client，未綁 = 自家
            "name": shop,
            "customer_name": (cust or {}).get("name"),
            "customer_id": (cust or {}).get("id"),
            "spend": kpi["spend"],
            "revenue": kpi["revenue"],
            "roas": roas,
            "target_roas": target_roas,
            "settle_day": (cust or {}).get("settle_day", 25),
            "status": status,
            "row_count": kpi["_meta"]["row_count"],
        })
    items.sort(key=lambda x: x["revenue"], reverse=True)
    return ok({"items": items, "total": len(items),
               "_meta": {"source": "real"}})


# ─────────────────────────── 高效時段熱力圖 ───────────────────────────

@app.get("/api/v1/heatmap")
def get_heatmap(account_id: str = "S001", category: str = "all"):
    days = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]
    cells = []
    for di, d in enumerate(days):
        for h in range(24):
            v = random.random()
            if h in (12, 21, 22):
                v = min(1, v + 0.4)
            if 2 <= h <= 6:
                v *= 0.3
            cells.append({"day": d, "hour": h, "roas": round(v * 5, 2), "level": _level(v)})
    return ok({"account_id": account_id, "category": category,
               "cells": cells,
               "peak_hours": [12, 13, 21, 22, 23]})


def _level(v: float) -> int:
    if v < 0.2: return 0
    if v < 0.4: return 1
    if v < 0.6: return 2
    if v < 0.8: return 3
    return 4


# ─────────────────────────── 活動行事曆（近期上傳 + 規則觸發） ───────────────────────────

@app.get("/api/v1/events")
def get_events_legacy():
    """以「最近活動」呈現：CSV 上傳、規則觸發。
    沒任何 CSV 時回空陣列。
    """
    items = []
    # 近期 CSV 上傳事件
    for u in ad_data_store.list_uploads()[:10]:
        s = u.get("summary") or {}
        is_lifetime = s.get("is_lifetime_report")
        items.append({
            "id": u["id"],
            "type": "upload",
            "name": f"📥 上傳 {u['filename']}",
            "tip": f"shop={u['shop']} · {s.get('row_count',0)} 筆"
                   + (f" · ⚠ {s.get('period_days')} 日彙總" if is_lifetime else " · ✓ 單日"),
            "at": u["uploaded_at"],
            "last_roas": s.get("roas"),
        })
    # 近期規則觸發紀錄（從 rules.json 的 last_triggered）
    for r in _load_rules():
        if r.get("last_triggered"):
            items.append({
                "id": r["id"],
                "type": "rule",
                "name": f"🤖 規則觸發：{r['name']}",
                "tip": f"累計觸發 {r.get('trigger_count', 0)} 次 · action: {r.get('action')}",
                "at": r["last_triggered"],
            })
    items.sort(key=lambda x: x.get("at", ""), reverse=True)
    return ok({"items": items[:15],
               "_meta": {"source": "real" if items else "empty",
                         "note": None if items else "尚未上傳 CSV，沒有活動紀錄"}})


# ─────────────────────────── AI 建議 ───────────────────────────

@app.get("/api/v1/suggestions")
def get_suggestions(type: str = "all", shop: str | None = None):
    """AI 調整建議 — 從真實 CSV 商品資料 + profit_config 產生具體可執行建議。
    不再用 mock。
    """
    if not ad_data_store.has_real_data():
        return ok({"items": [], "total": 0,
                   "_meta": {"source": "real",
                             "note": "尚未上傳 CSV，無法產生建議"}})

    # 取真實商品 + 客戶目標 ROAS
    products = ad_data_store.aggregate_products(shop=shop, period="month", limit=50)
    customers = _load_customers()
    shop_to_target = {}
    for c in customers:
        for sid in c.get("shop_ids", []):
            shop_to_target[sid] = c.get("target_roas", 3.5)

    items = []
    sid_counter = 0
    for p in products:
        sid_counter += 1
        roas = p.get("roas") or 0
        ctr = p.get("ctr") or 0
        spend = p.get("spend") or 0
        revenue = p.get("revenue") or 0
        orders = p.get("orders") or 0
        clicks = p.get("clicks") or 0
        target = shop_to_target.get(p.get("shop"), 3.5)
        product_name = (p.get("product_name") or p.get("product_id") or "(unknown)")[:40]
        shop_label = p.get("shop") or "—"

        # 規則 1：ROAS 低於目標 70% → 暫停 / 大幅降出價
        if 0 < roas < target * 0.7:
            items.append({
                "id": f"S-{sid_counter}-pause",
                "type": "出價",
                "title": f"{shop_label} — 「{product_name}」考慮暫停或大幅降出價",
                "reason": f"ROAS {roas} 遠低於目標 {target}（差 {round((target-roas)/target*100)}%）。"
                          f"近期花費 ${spend:,.0f}、營收 ${revenue:,.0f}，每花 1 元只回 {roas} 元。",
                "impact": {
                    "預估節省": f"${int(spend*0.7):,}/月",
                    "建議動作": "暫停 或 出價降 50%",
                    "目標 ROAS": f"{roas} → {target}",
                },
            })
        # 規則 2：ROAS 介於目標 70%-100% → 微調
        elif 0 < roas < target:
            items.append({
                "id": f"S-{sid_counter}-tune",
                "type": "出價",
                "title": f"{shop_label} — 「{product_name}」微調出價 / 排除負面詞",
                "reason": f"ROAS {roas} 略低於目標 {target}（差 {round((target-roas)/target*100)}%）。"
                          f"轉換率 {round(orders/clicks*100,2) if clicks else 0}%，可能有不精準流量。",
                "impact": {
                    "建議動作": "出價降 10-15% 或加負面詞",
                    "目標 ROAS": f"{roas} → {target}",
                },
            })
        # 規則 3：ROAS 超標 20% → 加碼
        elif roas >= target * 1.2:
            items.append({
                "id": f"S-{sid_counter}-scale",
                "type": "預算",
                "title": f"{shop_label} — 「{product_name}」加碼預算",
                "reason": f"ROAS {roas} 超標 {round((roas-target)/target*100)}%，"
                          f"近期帶來 ${revenue:,.0f} 營收 / {orders} 訂單，仍有放大空間。",
                "impact": {
                    "建議動作": "預算 +30%~+50%",
                    "預估營收": f"+${int(revenue*0.3):,}/月",
                    "目標": "持續監控 ROAS 不要降太多",
                },
            })

        # 規則 4：CTR 低於 1% → 廣告素材有問題
        if 0 < ctr < 1 and clicks > 50:
            items.append({
                "id": f"S-{sid_counter}-ctr",
                "type": "關鍵字",
                "title": f"{shop_label} — 「{product_name}」改廣告圖或標題",
                "reason": f"CTR 僅 {ctr:.2f}%（業界 2-3%），"
                          f"曝光被浪費。可能是封面圖、標題、定位不對。",
                "impact": {
                    "建議動作": "換主圖 + 重寫標題",
                    "預估 CTR": f"{ctr:.2f}% → 2.0%+",
                },
            })

    # 規則 5：根據預算燃燒狀況（從 budget pacing）找燒太快的 shop
    pacing_items = ad_data_store.aggregate_budget_pacing(shop=shop)
    for sh in pacing_items:
        if sh.get("burn_rate_pct", 0) > 110:
            items.append({
                "id": f"S-burn-{sh['shop_id']}",
                "type": "預算",
                "title": f"{sh['shop_name']} — 預算燒爆，下午晚上將無曝光",
                "reason": f"今日已燒 {sh['burn_rate_pct']}% 預算（${sh['burn_today']:,}），"
                          f"預計 {sh['predicted_depletion_hour']} 點燒完。",
                "impact": {
                    "建議動作": "立刻拉每日預算 +30% 或停某些低 ROAS 廣告",
                    "預估節省曝光": "晚上 19-23 點高峰時段",
                },
            })

    # 過濾類型
    if type != "all":
        items = [s for s in items if s["type"] == type]

    return ok({"items": items, "total": len(items),
               "_meta": {"source": "real", "shop": shop,
                         "based_on": f"{len(products)} 個真實商品的 KPI"}})


@app.post("/api/v1/suggestions/{sid}/apply")
def apply_suggestion(sid: str):
    return ok({"id": sid, "applied_at": datetime.now(timezone.utc).isoformat(), "status": "queued"})


# ─────────────────────────── 商品優化 ───────────────────────────

@app.get("/api/v1/products/rising")
def get_rising_products(shop: str | None = None, limit: int = Query(10, ge=1, le=50)):
    """上升商品 — 按 ROAS × 訂單數 排序的真實 CSV 商品。
    沒 CSV 時回空 array + 提示訊息。
    """
    if not ad_data_store.has_real_data():
        return ok({"items": [],
                   "_meta": {"source": "real",
                             "note": "尚未上傳 CSV，無法分析上升商品"}})
    products = ad_data_store.aggregate_products(shop=shop, period="month", limit=100)
    # 按 ROAS × orders 算「綜合分數」（簡單版）
    items = []
    for p in products:
        roas = p.get("roas") or 0
        orders = p.get("orders") or 0
        score = round(roas * (1 + orders / 50), 2)  # 訂單越多越加成
        suggestion = (
            "建議 +30% 預算" if roas >= 4 and orders >= 5 else
            "建議追加長尾關鍵字" if roas >= 3 and orders < 5 else
            "微調出價測試" if roas >= 2 else
            "考慮降出價或暫停"
        )
        items.append({
            "id": p["product_id"],
            "shop": p.get("shop") or "—",
            "product": p["product_name"],
            "clicks_total": p.get("clicks") or 0,
            "orders": orders,
            "roas": roas,
            "spend": p["spend"],
            "revenue": p["revenue"],
            "ctr": p.get("ctr") or 0,
            "current_budget": int(p["spend"] / 30) if p["spend"] else 0,  # 估每日預算
            "suggest": suggestion,
            "score": score,
        })
    items.sort(key=lambda x: x["score"], reverse=True)
    return ok({"items": items[:limit], "_meta": {"source": "real", "shop": shop}})


@app.get("/api/v1/products/optimized")
def get_optimized(shop: str | None = None):
    """投廣後優化數據 — 比較最近 30 天 vs 之前 30 天的真實表現。
    沒足夠歷史資料時，只回顯目前數據（無對比）。
    """
    if not ad_data_store.has_real_data():
        return ok({"items": [],
                   "_meta": {"source": "real",
                             "note": "尚未上傳 CSV，無法做優化前後比對"}})

    rows = ad_data_store._load()["rows"]
    if shop:
        rows = [r for r in rows if r.get("shop") == shop]
    if not rows:
        return ok({"items": [], "_meta": {"source": "real",
                                           "note": f"shop={shop} 沒資料"}})

    # 找近 30 天 vs 之前 30 天
    today = date.today()
    p1_since = today - timedelta(days=30)
    p2_since = today - timedelta(days=60)

    def in_period(r, since_d, until_d):
        d = r.get("date")
        if not d:
            # date=None 算進「近期」
            return until_d == today
        try:
            dd = date.fromisoformat(d)
        except ValueError:
            return False
        return since_d <= dd < until_d

    by_pid = {}
    for r in rows:
        pid = r.get("product_id")
        if not pid or pid in ("-", "—"):
            continue
        b = by_pid.setdefault(pid, {
            "product_id": pid,
            "product_name": r.get("product_name") or pid,
            "shop": r.get("shop"),
            "before": {"clicks": 0, "spend": 0, "revenue": 0, "orders": 0},
            "after":  {"clicks": 0, "spend": 0, "revenue": 0, "orders": 0},
        })
        if in_period(r, p1_since, today):
            target = b["after"]
        elif in_period(r, p2_since, p1_since):
            target = b["before"]
        else:
            continue
        target["clicks"]   += r.get("clicks") or 0
        target["spend"]    += r.get("spend") or 0
        target["revenue"]  += r.get("revenue") or 0
        target["orders"]   += r.get("orders") or 0

    def with_roas(d):
        d = dict(d)
        d["roas"] = round(d["revenue"] / d["spend"], 2) if d["spend"] else 0
        return d

    items = []
    for pid, b in by_pid.items():
        before, after = with_roas(b["before"]), with_roas(b["after"])
        # 只列「有 after 數據」的商品
        if after["spend"] == 0:
            continue
        # 算 lift（若 before 為 0，標 NEW）
        def lift_pct(after_v, before_v):
            if before_v == 0:
                return "NEW" if after_v > 0 else "-"
            return f"{round((after_v - before_v) / before_v * 100):+d}%"
        items.append({
            "id": pid,
            "shop": b["shop"],
            "product": b["product_name"],
            "before": before, "after": after,
            "lift": {
                "clicks":  lift_pct(after["clicks"],  before["clicks"]),
                "orders":  lift_pct(after["orders"],  before["orders"]),
                "revenue": lift_pct(after["revenue"], before["revenue"]),
                "roas":    f"{before['roas']} → {after['roas']}",
            },
        })
    items.sort(key=lambda x: x["after"]["revenue"], reverse=True)
    return ok({"items": items, "_meta": {"source": "real", "shop": shop}})


@app.get("/api/v1/actions")
def get_actions():
    return ok({"items": [
        {"freq":"每日","title":"檢視 ROAS < 1.5 的廣告組","desc":"紅色警示帳號優先排查，是出價過高、轉換頁差、還是受眾不對。"},
        {"freq":"每日","title":"監控當日花費燒太快的廣告","desc":"若中午前已燒掉 80% 預算，重新分配時段或調整出價策略。"},
        {"freq":"每週","title":"排除負面關鍵字","desc":"從搜尋詞報告中找出點擊高、轉換低的詞，加入排除清單。"},
        {"freq":"每週","title":"盤點上升中商品 → 加碼預算","desc":"點擊 / 加購趨勢上升 30% 以上的商品，立即追加廣告投資。"},
        {"freq":"每週","title":"測試新長尾關鍵字","desc":"每週至少投放 5 組新詞，找出競爭低、轉換高的藍海詞。"},
        {"freq":"每月","title":"檢討活動檔期 ROAS","desc":"比較大檔（雙11、母親節）vs 平日 ROAS，調整下次預算分配。"},
        {"freq":"每月","title":"更新受眾包與排除名單","desc":"將最近 30 天購買者加入相似受眾，舊客戶從新客廣告排除。"},
        {"freq":"每季","title":"重新評估出價策略","desc":"觀察整體競價環境變化，是否從 CPC 改為 ROAS 目標出價更划算。"},
        {"freq":"每季","title":"A/B 測試廣告素材","desc":"每季更新主圖與標題，避免素材疲乏導致 CTR 下降。"},
    ]})


# ─────────────────────────── 每週快照（投廣數據紀錄） ───────────────────────────
# 用 JSON 檔當儲存層，未來換 Firestore / SQLite 只要改這幾個函式

SNAP_FILE = ROOT / "snapshots.json"


def _load_snaps():
    if not SNAP_FILE.exists():
        return []
    return json.loads(SNAP_FILE.read_text(encoding="utf-8"))


def _save_snaps(snaps):
    SNAP_FILE.write_text(json.dumps(snaps, ensure_ascii=False, indent=2), encoding="utf-8")


@app.get("/api/v1/snapshots")
def list_snapshots():
    """列出所有歷史快照（最新在前）"""
    snaps = _load_snaps()
    return ok({
        "items": [{"id": s["id"], "week": s["week"], "taken_at": s["taken_at"],
                   "total_spend": s["total_spend"], "total_revenue": s["total_revenue"],
                   "overall_roas": s["overall_roas"], "account_count": len(s["accounts"])}
                  for s in sorted(snaps, key=lambda x: x["taken_at"], reverse=True)],
        "total": len(snaps),
    })


@app.post("/api/v1/snapshots/take")
def take_snapshot():
    """建立本週快照：抓當下 accounts 數據存檔"""
    snaps = _load_snaps()
    now = datetime.now(timezone.utc)
    week = now.strftime("%G-W%V")
    accounts = get_accounts()["data"]["items"]
    total_spend = sum(a["spend"] for a in accounts)
    total_revenue = sum(a["revenue"] for a in accounts)
    snap = {
        "id": f"SNAP-{now.strftime('%Y%m%d%H%M%S')}",
        "week": week,
        "taken_at": now.isoformat() + "Z",
        "total_spend": total_spend,
        "total_revenue": total_revenue,
        "overall_roas": round(total_revenue / total_spend, 2) if total_spend else 0,
        "accounts": accounts,
    }
    snaps.append(snap)
    _save_snaps(snaps)
    return ok({"id": snap["id"], "week": snap["week"], "taken_at": snap["taken_at"]})


@app.get("/api/v1/snapshots/{snap_id}/csv")
def export_snapshot_csv(snap_id: str):
    """下載某一週快照為 CSV（給代操業者每週存檔用）"""
    snaps = _load_snaps()
    snap = next((s for s in snaps if s["id"] == snap_id), None)
    if not snap:
        return {"data": None, "error": {"code": "NOT_FOUND", "message": "快照不存在"}}
    buf = io.StringIO()
    buf.write("﻿")  # UTF-8 BOM, Excel 開啟才不會亂碼
    w = csv.writer(buf)
    w.writerow(["週次", snap["week"], "建立時間", snap["taken_at"]])
    w.writerow([])
    w.writerow(["類型", "店家名稱", "花費", "營收", "ROAS", "預算使用率%", "月結算日", "狀態"])
    for a in snap["accounts"]:
        w.writerow([a["type"], a["name"], a["spend"], a["revenue"], a["roas"],
                    a["budget_used"], a["settle_day"], a["status"]])
    w.writerow([])
    w.writerow(["合計", "", snap["total_spend"], snap["total_revenue"], snap["overall_roas"]])
    buf.seek(0)
    fname = f"shopee_ad_snapshot_{snap['week']}.csv"
    return StreamingResponse(
        iter([buf.getvalue().encode("utf-8")]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.get("/api/v1/snapshots/compare")
def compare_snapshots(weeks: int = Query(4, ge=2, le=12)):
    """週對週趨勢：給「投廣優化建議」用的歷史數據"""
    snaps = sorted(_load_snaps(), key=lambda x: x["taken_at"])[-weeks:]
    return ok({
        "weeks": [s["week"] for s in snaps],
        "spend": [s["total_spend"] for s in snaps],
        "revenue": [s["total_revenue"] for s in snaps],
        "roas": [s["overall_roas"] for s in snaps],
    })


# ─────────────────────────── 競品監控 ───────────────────────────

@app.get("/api/v1/competitor/keywords")
def get_competitor_keywords():
    """主要關鍵字排名追蹤"""
    return ok({"items": [
        {"keyword":"無線耳機",     "my_rank":3,  "rank_trend":[8,7,5,5,4,4,3],   "top_competitor":"耳機王國", "comp_bid":12.5, "search_volume":48000},
        {"keyword":"快充充電器",   "my_rank":1,  "rank_trend":[5,4,3,2,2,1,1],   "top_competitor":"3C 達人館", "comp_bid":8.8,  "search_volume":32000},
        {"keyword":"保濕精華液",   "my_rank":7,  "rank_trend":[10,9,9,8,8,7,7],  "top_competitor":"美妝實驗室", "comp_bid":15.2, "search_volume":28000},
        {"keyword":"登山背包",     "my_rank":12, "rank_trend":[15,14,14,13,12,12,12], "top_competitor":"野營王", "comp_bid":6.5, "search_volume":15000},
        {"keyword":"寶寶副食品",   "my_rank":2,  "rank_trend":[6,5,4,3,3,2,2],   "top_competitor":"母嬰天地", "comp_bid":9.8,  "search_volume":42000},
        {"keyword":"貓咪零食",     "my_rank":4,  "rank_trend":[6,6,5,5,4,4,4],   "top_competitor":"毛孩專賣", "comp_bid":7.2,  "search_volume":22000},
    ]})


@app.get("/api/v1/competitor/price-distribution")
def get_competitor_prices(category: str = "3C"):
    """同類目價格分布"""
    samples = {
        "3C":   {"buckets":["<500","500-1k","1k-2k","2k-5k","5k+"], "competitors":[12,28,35,18,7], "ours":[2,5,4,1,0]},
        "美妝": {"buckets":["<300","300-600","600-1k","1k-2k","2k+"], "competitors":[8,22,30,25,15], "ours":[1,3,4,2,1]},
        "食品": {"buckets":["<200","200-400","400-800","800-1.5k","1.5k+"], "competitors":[18,32,28,15,7], "ours":[3,6,4,2,0]},
    }
    return ok({"category": category, **samples.get(category, samples["3C"])})


# ─────────────────────────── 單品出價 / 預算調整 ───────────────────────────

ADJUST_FILE = ROOT / "adjustments.json"


def _load_adjustments():
    if not ADJUST_FILE.exists():
        return []
    return json.loads(ADJUST_FILE.read_text(encoding="utf-8"))


def _save_adjustments(items):
    ADJUST_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


class AdjustReq(BaseModel):
    product_id: str
    product_name: str
    shop: str
    action_type: str       # "bid" | "budget" | "both"
    old_bid: float | None = None
    new_bid: float | None = None
    old_budget: float | None = None
    new_budget: float | None = None
    note: str | None = None


@app.get("/api/v1/products/{pid}/ad-settings")
def get_ad_settings(pid: str):
    """讀取某商品目前廣告設定（mock）"""
    presets = {
        "P1": {"current_bid": 8.5,  "current_budget": 1200, "current_roas": 3.5, "ad_type": "搜尋廣告"},
        "P2": {"current_bid": 6.2,  "current_budget": 800,  "current_roas": 4.1, "ad_type": "搜尋廣告"},
        "P3": {"current_bid": 4.8,  "current_budget": 600,  "current_roas": 5.2, "ad_type": "關聯廣告"},
        "P4": {"current_bid": 7.5,  "current_budget": 1500, "current_roas": 3.8, "ad_type": "搜尋廣告"},
        "P5": {"current_bid": 9.0,  "current_budget": 900,  "current_roas": 4.5, "ad_type": "搜尋廣告"},
        "P6": {"current_bid": 5.5,  "current_budget": 550,  "current_roas": 5.8, "ad_type": "關聯廣告"},
        "P7": {"current_bid": 4.2,  "current_budget": 400,  "current_roas": 4.0, "ad_type": "搜尋廣告"},
    }
    return ok(presets.get(pid, {"current_bid": 5.0, "current_budget": 500, "current_roas": 3.0, "ad_type": "搜尋廣告"}))


@app.post("/api/v1/products/adjust")
def submit_adjust(req: AdjustReq):
    """記錄一筆調整意圖（半自動模式：儀表板記錄 → 代操手動到後台改 → 標記完成）"""
    items = _load_adjustments()
    item = {
        "id": f"ADJ-{int(time.time()*1000)}",
        "product_id": req.product_id,
        "product_name": req.product_name,
        "shop": req.shop,
        "action_type": req.action_type,
        "old_bid": req.old_bid, "new_bid": req.new_bid,
        "old_budget": req.old_budget, "new_budget": req.new_budget,
        "note": req.note,
        "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "executed_at": None,
    }
    items.append(item)
    _save_adjustments(items)
    return ok(item)


@app.get("/api/v1/adjustments")
def list_adjustments(status: str = "all"):
    """列出所有調整任務（pending = 待操作員到後台改）"""
    items = _load_adjustments()
    if status != "all":
        items = [i for i in items if i["status"] == status]
    items.sort(key=lambda x: x["created_at"], reverse=True)
    return ok({"items": items, "total": len(items)})


@app.post("/api/v1/adjustments/{adj_id}/done")
def mark_adjustment_done(adj_id: str):
    """操作員到賣家中心改完後，回來標記完成"""
    items = _load_adjustments()
    for i in items:
        if i["id"] == adj_id:
            i["status"] = "done"
            i["executed_at"] = datetime.now(timezone.utc).isoformat()
            _save_adjustments(items)
            return ok(i)
    raise HTTPException(404, "not found")


# ─────────────────────────── Shopee OAuth 流程 ───────────────────────────

SHOPEE_PARTNER_ID = os.getenv("SHOPEE_PARTNER_ID", "MOCK_PARTNER_123456")
SHOPEE_PARTNER_KEY = os.getenv("SHOPEE_PARTNER_KEY", "mock_partner_key_for_demo_only")
SHOPEE_AUTH_HOST = os.getenv("SHOPEE_AUTH_HOST", "https://partner.shopeemobile.com")
SHOPEE_REDIRECT = os.getenv("SHOPEE_REDIRECT", "http://localhost:8765/api/v1/auth/shopee/callback")
SHOPEE_MOCK_MODE = SHOPEE_PARTNER_ID == "MOCK_PARTNER_123456"

CONNECTIONS_FILE = ROOT / "shopee_connections.json"


def _load_conns():
    if not CONNECTIONS_FILE.exists():
        return []
    return json.loads(CONNECTIONS_FILE.read_text(encoding="utf-8"))


def _save_conns(items):
    CONNECTIONS_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def _shopee_sign(path: str, ts: int, access_token: str = "", shop_id: str = "") -> str:
    """蝦皮 Open API 標準簽章 — HMAC-SHA256(partner_id + path + ts + token + shop_id)"""
    base = f"{SHOPEE_PARTNER_ID}{path}{ts}{access_token}{shop_id}".encode()
    return hmac.new(SHOPEE_PARTNER_KEY.encode(), base, hashlib.sha256).hexdigest()


@app.get("/api/v1/auth/shopee/url")
def shopee_auth_url():
    """產生賣家授權 URL → 引導賣家點擊登入並同意授權"""
    path = "/api/v2/shop/auth_partner"
    ts = int(time.time())
    sign = _shopee_sign(path, ts)
    qs = urlencode({"partner_id": SHOPEE_PARTNER_ID, "timestamp": ts,
                    "sign": sign, "redirect": SHOPEE_REDIRECT})
    real_url = f"{SHOPEE_AUTH_HOST}{path}?{qs}"
    if SHOPEE_MOCK_MODE:
        # demo 模式直接導去本地 mock callback，模擬蝦皮跳回來
        mock_shop = random.randint(100000, 999999)
        mock_code = f"MOCK_CODE_{int(time.time())}"
        return ok({"url": f"/api/v1/auth/shopee/callback?code={mock_code}&shop_id={mock_shop}",
                   "mock": True, "real_would_be": real_url})
    return ok({"url": real_url, "mock": False})


@app.get("/api/v1/auth/shopee/callback")
def shopee_callback(code: str, shop_id: str):
    """賣家同意授權後蝦皮會帶 code 跳回這裡 → 用 code 換 access_token"""
    # 真實流程：POST /api/v2/auth/token/get { code, shop_id, partner_id } 帶簽章
    # mock 模式直接生假 token
    now = datetime.now(timezone.utc)
    conn = {
        "shop_id": shop_id,
        "shop_name": f"店家 {shop_id}（mock）" if SHOPEE_MOCK_MODE else f"shop_{shop_id}",
        "access_token": f"MOCK_AT_{shop_id}_{int(time.time())}" if SHOPEE_MOCK_MODE else "REAL_TOKEN_HERE",
        "refresh_token": f"MOCK_RT_{shop_id}",
        "expires_at": (now + timedelta(hours=4)).isoformat() + "Z",
        "connected_at": now.isoformat() + "Z",
        "mock": SHOPEE_MOCK_MODE,
    }
    conns = _load_conns()
    conns = [c for c in conns if c["shop_id"] != shop_id]
    conns.append(conn)
    _save_conns(conns)
    return RedirectResponse(url=f"/?connected={shop_id}", status_code=302)


@app.get("/api/v1/auth/shopee/connections")
def list_connections():
    """已綁定的賣家店家列表"""
    conns = _load_conns()
    safe = [{"shop_id": c["shop_id"], "shop_name": c["shop_name"],
             "expires_at": c["expires_at"], "connected_at": c["connected_at"],
             "mock": c.get("mock", False)} for c in conns]
    return ok({"items": safe, "total": len(safe), "mock_mode": SHOPEE_MOCK_MODE})


@app.delete("/api/v1/auth/shopee/{shop_id}")
def disconnect_shopee(shop_id: str):
    conns = _load_conns()
    conns = [c for c in conns if c["shop_id"] != shop_id]
    _save_conns(conns)
    return ok({"removed": shop_id})


# ─────────────────────────── Shopee 真實 API 同步 ───────────────────────────

SHOPEE_PRODUCTS_FILE = ROOT / "shopee_products.json"


def _load_shopee_products() -> dict:
    if not SHOPEE_PRODUCTS_FILE.exists():
        return {}
    return json.loads(SHOPEE_PRODUCTS_FILE.read_text(encoding="utf-8"))


def _save_shopee_products(data: dict) -> None:
    SHOPEE_PRODUCTS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


@app.get("/api/v1/shopee/status")
def shopee_status():
    """Shopee Open API 設定狀態（給 UI 判斷要不要顯示同步按鈕）"""
    cfg = shopee_client.config_status()
    cfg["connected_shops"] = len(_load_conns())
    return ok(cfg)


@app.post("/api/v1/shopee/products/sync")
def shopee_sync_products(shop_id: str = Query(...)):
    """從蝦皮拉商品列表 + 詳情 → 存到 shopee_products.json
    沒設 partner_id 時自動 mock。
    """
    shop_id = _safe_id(shop_id, "shop_id")
    conns = _load_conns()
    conn = next((c for c in conns if c["shop_id"] == shop_id), None)
    if not conn:
        raise HTTPException(404, f"shop {shop_id} 未綁定，請先到 OAuth 連結")
    try:
        result = shopee_client.sync_products(shop_id, conn["access_token"])
    except Exception as e:
        raise HTTPException(502, f"蝦皮 API 呼叫失敗：{e}")
    # 持久化
    store = _load_shopee_products()
    store[shop_id] = result
    _save_shopee_products(store)
    return ok({
        "shop_id": shop_id,
        "synced": result["total"],
        "mode": result["mode"],
        "sample": result["items"][:3],
    })


@app.get("/api/v1/shopee/products")
def shopee_list_products(shop_id: str | None = None):
    """列出已同步的商品（給商品健康度補 rating/stock/reviews 用）"""
    store = _load_shopee_products()
    if shop_id:
        return ok(store.get(shop_id, {"items": [], "total": 0}))
    # 全部 shop 合併
    all_items = []
    for sid, data in store.items():
        for item in data.get("items", []):
            all_items.append({**item, "_shop_id": sid})
    return ok({"items": all_items, "total": len(all_items),
               "shops_synced": list(store.keys())})


# ─────────────────────────── 廣告 CSV 上傳 ───────────────────────────

UPLOADS_DIR = ROOT / "uploads"
UPLOADS_DIR.mkdir(exist_ok=True)
UPLOADS_INDEX = ROOT / "uploads_index.json"


def _load_uploads():
    if not UPLOADS_INDEX.exists():
        return []
    return json.loads(UPLOADS_INDEX.read_text(encoding="utf-8"))


def _save_uploads(items):
    UPLOADS_INDEX.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


import re as _re
_SAFE_ID_RE = _re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
# shop label：允許中文/英數/空白/常見符號；禁止路徑分隔符、控制字元、引號
_FORBIDDEN_LABEL_RE = _re.compile(r"[\x00-\x1f\x7f/\\<>\"'`\r\n\t]")


def _safe_id(value: str, label: str) -> str:
    if not value or not _SAFE_ID_RE.match(value):
        raise HTTPException(400, f"{label} 格式不合法（只允許英數、底線、連字號，最長 64 字元）")
    return value


def _safe_label(value: str, label: str, max_len: int = 64) -> str:
    """較寬鬆的識別字串檢查：允許中文/英數/空白/常見標點，禁止路徑符與控制字元。"""
    v = (value or "").strip()
    if not v or len(v) > max_len:
        raise HTTPException(400, f"{label} 不可為空、長度需 ≤ {max_len}")
    if _FORBIDDEN_LABEL_RE.search(v):
        raise HTTPException(400, f"{label} 包含禁止字元（路徑符號、引號、控制字元）")
    return v


@app.post("/api/v1/upload/ad-report")
async def upload_ad_report(
    shop: str = Form(...),
    file: UploadFile = File(...),
    force: bool = Form(False),
):
    """接收賣家中心匯出的廣告報表 CSV → 解析、正規化、持久化、立即聚合可用

    force=True：即使偵測到重複（同 shop + 同 filename + 同期間）仍上傳
    """
    shop = _safe_label(shop, "shop")
    # 副檔名白名單（防止上傳非 CSV）
    fname = (file.filename or "").lower()
    if not (fname.endswith(".csv") or fname.endswith(".tsv") or fname.endswith(".txt")):
        raise HTTPException(400, "只接受 .csv / .tsv / .txt 檔案")
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "空檔案")
    if len(raw) > 50 * 1024 * 1024:  # 50 MB 上限
        raise HTTPException(413, "檔案超過 50MB 上限")

    try:
        result = ad_report_parser.parse_csv(raw)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(400, f"解析失敗：{type(e).__name__}: {e}")

    if not result.rows:
        raise HTTPException(400,
            f"CSV 已解碼（{result.encoding}）但沒有可用資料列。"
            f"原始欄位：{result.raw_columns[:8]}，"
            f"已映射欄位：{[k for k,v in result.column_map.items() if v]}")

    # 偵測重複（同 shop + 同檔名 + 同期間）
    duplicate = ad_data_store.find_duplicate_upload(
        shop=shop, filename=file.filename,
        period_start=result.summary.get("report_period_start"),
        period_end=result.summary.get("report_period_end"),
    )
    if duplicate and not force:
        raise HTTPException(409, {
            "error": "duplicate_upload",
            "message": (
                f"偵測到重複上傳！同樣的「{file.filename}」"
                f"({result.summary.get('report_period_start')} ~ "
                f"{result.summary.get('report_period_end')}) "
                f"已於 {duplicate['uploaded_at'][:19].replace('T',' ')} 上傳過。"
                "如要強制再上傳請帶 force=true（會造成數字加倍）。"
            ),
            "existing_upload_id": duplicate["id"],
        })

    upload_id = f"UP-{int(time.time()*1000)}"
    (UPLOADS_DIR / f"{upload_id}.csv").write_bytes(raw)

    meta = ad_data_store.save_upload(upload_id, shop, file.filename, result)

    # 同時更新舊版 uploads_index.json（向後相容）
    legacy_items = _load_uploads()
    legacy_items.append({
        "id": upload_id, "shop": shop, "filename": file.filename,
        "uploaded_at": meta["uploaded_at"],
        "row_count": result.summary.get("row_count", 0),
        "columns": result.raw_columns, "size_bytes": len(raw),
    })
    _save_uploads(legacy_items)

    return ok({
        **meta,
        "size_bytes": len(raw),
        "preview": result.rows[:5],
        # 向後相容欄位
        "row_count": result.summary.get("row_count", 0),
        "columns": result.raw_columns,
    })


@app.get("/api/v1/uploads")
def list_uploads():
    """合併兩個來源：新版 ad_reports.json 的 uploads + 舊版 uploads_index.json"""
    new_items = ad_data_store.list_uploads()
    if new_items:
        return ok({"items": new_items, "total": len(new_items)})
    items = sorted(_load_uploads(), key=lambda x: x["uploaded_at"], reverse=True)
    return ok({"items": items, "total": len(items)})


@app.delete("/api/v1/uploads/{upload_id}")
def delete_upload(upload_id: str):
    upload_id = _safe_id(upload_id, "upload_id")
    deleted = ad_data_store.delete_upload(upload_id)
    if not deleted:
        raise HTTPException(404, "找不到該上傳紀錄")
    # 同時清掉舊版索引
    items = [u for u in _load_uploads() if u.get("id") != upload_id]
    _save_uploads(items)
    # 清原始 CSV 檔
    p = UPLOADS_DIR / f"{upload_id}.csv"
    if p.exists():
        try: p.unlink()
        except OSError: pass
    return ok({"id": upload_id, "deleted": True})


# ─────────────────────────── 真實廣告數據聚合 ───────────────────────────

@app.get("/api/v1/ad-data/status")
def ad_data_status():
    """前端用：判斷有沒有真實資料、可切換 source。"""
    uploads = ad_data_store.list_uploads()
    return ok({
        "has_real_data": ad_data_store.has_real_data(),
        "upload_count": len(uploads),
        "shops": ad_data_store.list_shops_with_data(),
        "latest_upload": uploads[0] if uploads else None,
    })


@app.get("/api/v1/ad-data/products")
def ad_data_products(
    shop: str | None = None,
    period: str = Query("month", pattern="^(yesterday|week|month)$"),
    limit: int = Query(50, ge=1, le=500),
):
    items = ad_data_store.aggregate_products(shop=shop, period=period, limit=limit)
    return ok({"items": items, "total": len(items),
               "_meta": {"source": "real", "shop": shop, "period": period}})


@app.get("/api/v1/keywords/performance")
def keyword_performance(
    shop: str | None = None,
    period: str = Query("month", pattern="^(yesterday|week|month)$"),
    limit: int = Query(100, ge=1, le=500),
):
    """關鍵字真實表現（從上傳的關鍵字/版位 CSV 聚合）。沒資料 → items=[]。"""
    items = ad_data_store.aggregate_keywords(shop=shop, period=period, limit=limit)
    return ok({
        "items": items, "total": len(items),
        "_meta": {
            "source": "real" if items else "empty",
            "shop": shop, "period": period,
            "note": None if items else "尚未上傳含「關鍵字」欄的 CSV（蝦皮匯出選『關鍵字/版位層級數據』）",
        },
    })


@app.get("/api/v1/placements/performance")
def placement_performance(
    shop: str | None = None,
    period: str = Query("month", pattern="^(yesterday|week|month)$"),
    limit: int = Query(100, ge=1, le=500),
):
    """版位真實表現（從上傳的關鍵字/版位 CSV 聚合）。"""
    items = ad_data_store.aggregate_placements(shop=shop, period=period, limit=limit)
    return ok({
        "items": items, "total": len(items),
        "_meta": {
            "source": "real" if items else "empty",
            "shop": shop, "period": period,
            "note": None if items else "尚未上傳含「版位」欄的 CSV",
        },
    })


# ─────────────────────────── 資料夾自動上傳 watcher ───────────────────────────

@app.get("/api/v1/watcher/status")
def watcher_status():
    return ok(csv_watcher.status())


@app.post("/api/v1/watcher/start")
def watcher_start(scan_interval: int = Query(5, ge=1, le=3600)):
    return ok(csv_watcher.start(scan_interval=scan_interval))


@app.post("/api/v1/watcher/stop")
def watcher_stop():
    return ok(csv_watcher.stop())


# ─────────────────────────── 蝦皮 v4 公開 API + Google Trends ───────────────────────────

@app.get("/api/v1/external/status")
def external_status():
    """外部 API 整合狀態"""
    return ok({
        "shopee_public": shopee_public_api.is_available(),
        "google_trends": google_trends_client.is_available(),
        "shopee_partner": shopee_client.config_status(),
    })


@app.get("/api/v1/external/shopee/item")
def external_shopee_item(shop_id: int, item_id: int):
    """從蝦皮 v4 公開 API 抓商品真實資料（rating/stock/sold/reviews）"""
    try:
        return ok(shopee_public_api.fetch_item_info(shop_id, item_id))
    except Exception as e:
        raise HTTPException(502, f"蝦皮 API: {e}")


@app.post("/api/v1/external/shopee/sync-health")
def external_shopee_sync_health(shop_id: int = Query(...)):
    """批次抓 store 中所有商品的真實 rating/stock/reviews（給商品健康度頁用）"""
    if not ad_data_store.has_real_data():
        return ok({"updated": 0, "errors": [], "note": "尚未上傳 CSV"})
    sales = ad_data_store.aggregate_product_sales(days=90)
    item_ids = []
    for pid in sales.keys():
        try:
            item_ids.append((shop_id, int(pid)))
        except (ValueError, TypeError):
            continue
    if not item_ids:
        return ok({"updated": 0, "errors": [],
                   "note": "找不到數字格式的 product_id"})
    results = shopee_public_api.fetch_items_batch(item_ids)
    success = [r for r in results if "error" not in r]
    errors = [r for r in results if "error" in r]
    return ok({
        "updated": len(success),
        "errors": errors,
        "items": success,
    })


@app.get("/api/v1/external/shopee/search")
def external_shopee_search(keyword: str, limit: int = Query(30, ge=1, le=60),
                           by: str = Query("sales", pattern="^(relevancy|pop|ctime|sales|price_asc|price_desc)$")):
    """蝦皮搜尋（給競品監控用）"""
    try:
        results = shopee_public_api.search_products(keyword, limit=limit, by=by)
        return ok({"keyword": keyword, "items": results, "total": len(results),
                   "_meta": {"source": "shopee_v4_public"}})
    except Exception as e:
        raise HTTPException(502, f"蝦皮搜尋: {e}")


@app.get("/api/v1/external/trends/daily")
def external_trends_daily(geo: str = "TW"):
    """Google Trends 今日熱搜（給關鍵字研究頁用）"""
    try:
        return ok({"items": google_trends_client.daily_trends(geo=geo),
                   "geo": geo, "_meta": {"source": "google_trends_rss"}})
    except Exception as e:
        raise HTTPException(502, f"Google Trends: {e}")


@app.get("/api/v1/external/trends/explore")
def external_trends_explore(keyword: str, geo: str = "TW"):
    """Google Trends 關鍵字熱度探索（需 pytrends 才完整）"""
    try:
        return ok(google_trends_client.explore_keyword(keyword, geo=geo))
    except Exception as e:
        raise HTTPException(502, f"Google Trends: {e}")


# ─────────────────────────── AI 顧問 ───────────────────────────

@app.get("/api/v1/ai/status")
def ai_status():
    return ok(ai_advisor.config_status())


@app.post("/api/v1/ai/insights")
def ai_insights(body: dict | None = None):
    """產生 AI 廣告優化建議。
    body (optional): {customer_id?, shop?, period?}
    """
    body = body or {}
    customer_id = body.get("customer_id")
    shop = body.get("shop")
    period = body.get("period", "month")

    customer = None
    shop_filter = shop
    if customer_id:
        customer = next((c for c in _load_customers() if c["id"] == customer_id), None)
        if customer and not shop_filter:
            shop_ids = customer.get("shop_ids") or []
            if shop_ids:
                shop_filter = shop_ids[0]

    # 收集 dashboard 資料（reuse 現有聚合函式）
    if ad_data_store.has_real_data():
        kpi = ad_data_store.aggregate_kpi(shop=shop_filter, period=period)
        profit_cfg = _load_profit_cfg()
        profit = ad_data_store.aggregate_profit(
            shop=shop_filter, period=period, profit_config=profit_cfg)
        top_products = ad_data_store.aggregate_products(
            shop=shop_filter, period=period, limit=10)
    else:
        kpi = {"spend": 0, "revenue": 0, "roas": 0, "ctr": 0, "orders": 0,
               "target_roas": 3.5}
        profit = None
        top_products = []

    rules = _load_rules()
    rules_report = rules_engine.apply_rules(rules, dry_run=True, shop=shop_filter)
    triggered_rules = [
        {"rule_name": t.rule_name, "rule": t.rule_name,
         "action": t.action, "affected_count": len(t.matched_products)}
        for t in rules_report.triggered
    ]

    if customer:
        kpi["target_roas"] = customer.get("target_roas", 3.5)

    report_data = {
        "kpi": kpi,
        "profit": profit,
        "top_products": top_products,
        "triggered_rules": triggered_rules,
    }
    insights = ai_advisor.generate_insights(report_data, customer=customer)
    insights["_input"] = {
        "customer_id": customer_id, "shop": shop_filter,
        "period": period,
        "data_summary": {
            "kpi_revenue": kpi.get("revenue"),
            "products_count": len(top_products),
            "rules_triggered": len(triggered_rules),
        },
    }
    return ok(insights)


@app.post("/api/v1/watcher/scan-now")
def watcher_scan_now():
    res = csv_watcher.scan_once()
    return ok({
        "scanned": res.scanned,
        "succeeded": res.succeeded,
        "failed": res.failed,
        "files": [vars(f) for f in res.files],
    })


class WatchDirReq(BaseModel):
    path: str


@app.post("/api/v1/watcher/dirs/add")
def watcher_add_dir(body: WatchDirReq):
    """新增監看目錄（例如 Google Drive 同步路徑）"""
    try:
        return ok(csv_watcher.add_watch_dir(body.path))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/v1/watcher/dirs/remove")
def watcher_remove_dir(body: WatchDirReq):
    return ok(csv_watcher.remove_watch_dir(body.path))


# ─────────────────────────── 競品設定 ───────────────────────────

COMPETITORS_FILE = ROOT / "competitors.json"


def _load_competitors():
    if not COMPETITORS_FILE.exists():
        return []
    return json.loads(COMPETITORS_FILE.read_text(encoding="utf-8"))


def _save_competitors(items):
    COMPETITORS_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


class CompetitorReq(BaseModel):
    shop_id: str | None = None
    shop_name: str
    shop_url: str | None = None
    category: str | None = None
    notes: str | None = None
    track_keywords: list[str] = []


@app.get("/api/v1/competitors/list")
def get_competitors_list():
    items = _load_competitors()
    return ok({"items": items, "total": len(items)})


@app.post("/api/v1/competitors/list")
def add_competitor(req: CompetitorReq):
    items = _load_competitors()
    item = {
        "id": f"COMP-{int(time.time()*1000)}",
        "shop_id": req.shop_id,
        "shop_name": req.shop_name,
        "shop_url": req.shop_url,
        "category": req.category,
        "notes": req.notes,
        "track_keywords": req.track_keywords,
        "added_at": datetime.now(timezone.utc).isoformat(),
        # 真實情境：定期跑 cron 抓 Open API 拿到的數據快照（mock）
        "last_snapshot": {
            "product_count": random.randint(50, 500),
            "avg_price": random.randint(200, 3000),
            "monthly_orders": random.randint(100, 5000),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
    }
    items.append(item)
    _save_competitors(items)
    return ok(item)


@app.delete("/api/v1/competitors/list/{cid}")
def remove_competitor(cid: str):
    items = _load_competitors()
    items = [i for i in items if i["id"] != cid]
    _save_competitors(items)
    return ok({"removed": cid})


# ─────────────────────────── 真實毛利計算（P0） ───────────────────────────
# ROAS 高 ≠ 賺錢，要扣商品成本 + 運費 + 平台抽成 + 刷卡費

PROFIT_CONFIG_FILE = ROOT / "profit_config.json"

DEFAULT_PROFIT_CONFIG = {
    "P1": {"cost": 280, "shipping": 60,  "platform_fee_rate": 0.055, "payment_fee_rate": 0.03, "selling_price": 690},
    "P2": {"cost": 420, "shipping": 80,  "platform_fee_rate": 0.055, "payment_fee_rate": 0.03, "selling_price": 980},
    "P3": {"cost": 850, "shipping": 100, "platform_fee_rate": 0.055, "payment_fee_rate": 0.03, "selling_price": 1880},
    "P4": {"cost": 180, "shipping": 60,  "platform_fee_rate": 0.055, "payment_fee_rate": 0.03, "selling_price": 480},
    "P5": {"cost": 320, "shipping": 80,  "platform_fee_rate": 0.055, "payment_fee_rate": 0.03, "selling_price": 890},
    "P6": {"cost": 95,  "shipping": 60,  "platform_fee_rate": 0.055, "payment_fee_rate": 0.03, "selling_price": 280},
    "P7": {"cost": 60,  "shipping": 40,  "platform_fee_rate": 0.055, "payment_fee_rate": 0.03, "selling_price": 180},
}


def _load_profit_cfg():
    if not PROFIT_CONFIG_FILE.exists():
        return dict(DEFAULT_PROFIT_CONFIG)
    return json.loads(PROFIT_CONFIG_FILE.read_text(encoding="utf-8"))


def _save_profit_cfg(cfg):
    PROFIT_CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


class ProfitCfg(BaseModel):
    cost: float
    shipping: float = 0           # 每單固定運費 (NT$)
    shipping_rate: float = 0      # 若 > 0，用「營收 × shipping_rate」取代 shipping × orders
    platform_fee_rate: float
    payment_fee_rate: float
    selling_price: float


@app.get("/api/v1/profit/config")
def get_profit_configs(shop: str | None = None, days: int = Query(90, ge=1, le=365)):
    """回傳 profit_config + 「CSV 中存在但 config 沒設定」的商品 stub（含建議成本）

    shop: 只看該 shop 的商品（給客戶切換用）；不傳則看全部
    days: 抓近 N 天 CSV 資料（蝦皮 lifetime 報表期間常 30/60/90，預設 90）
    """
    cfg = _load_profit_cfg()
    sales = ad_data_store.aggregate_product_sales(shop=shop, days=days)
    missing = []
    for pid, m in sales.items():
        if pid in cfg:
            continue
        avg_price = round(m["revenue"] / m["orders"]) if m["orders"] else 0
        missing.append({
            "product_id": pid,
            "product_name": m["product_name"],
            "shop": m["shop"],
            "orders": m["orders"],
            "revenue": round(m["revenue"], 2),
            "period_days": days,
            "suggested": {
                "selling_price": avg_price,
                "cost": round(avg_price * 0.5),
                "shipping": 60,
                "platform_fee_rate": 0.055,
                "payment_fee_rate": 0.03,
            },
        })
    return ok({
        "items": cfg,
        "missing_in_csv": missing,
        "_meta": {"configured": len(cfg), "missing": len(missing),
                  "total_in_csv": len(sales),
                  "shop": shop, "period_days": days},
    })


@app.delete("/api/v1/profit/config/{pid}")
def delete_profit_cfg(pid: str):
    cfg = _load_profit_cfg()
    if pid not in cfg:
        raise HTTPException(404, "找不到此商品設定")
    cfg.pop(pid)
    _save_profit_cfg(cfg)
    return ok({"removed": pid})


@app.put("/api/v1/profit/config/{pid}")
def update_profit_cfg(pid: str, cfg: ProfitCfg):
    all_cfg = _load_profit_cfg()
    all_cfg[pid] = cfg.model_dump()
    _save_profit_cfg(all_cfg)
    return ok(all_cfg[pid])


@app.get("/api/v1/profit/summary")
def get_profit_summary(
    period: str = Query("week", pattern="^(yesterday|week|month)$"),
    source: str = Query("auto", pattern="^(auto|mock|real)$"),
    shop: str | None = None,
):
    """整體真實獲利：營收 - 廣告 - 成本 - 運費 - 平台抽成 - 刷卡費

    source=real：用 CSV 真實 revenue/ad_spend × profit_config.json 的成本參數計算
    source=mock：用估算比例（毛利 38%）
    source=auto：有真實資料就用真實
    """
    use_real = source == "real" or (source == "auto" and ad_data_store.has_real_data())
    if use_real and ad_data_store.has_real_data():
        result = ad_data_store.aggregate_profit(
            shop=shop, period=period, profit_config=_load_profit_cfg(),
        )
        return ok(result)

    revenue = {"yesterday": 196800, "week": 1142800, "month": 4520000}[period]
    ad_spend = {"yesterday": 46920, "week": 285420, "month": 1180600}[period]
    cogs = round(revenue * 0.42)
    shipping_total = round(revenue * 0.06)
    platform_fee = round(revenue * 0.055)
    payment_fee = round(revenue * 0.03)
    net_profit = revenue - ad_spend - cogs - shipping_total - platform_fee - payment_fee
    return ok({
        "period": period, "revenue": revenue, "ad_spend": ad_spend,
        "cogs": cogs, "shipping_total": shipping_total,
        "platform_fee": platform_fee, "payment_fee": payment_fee,
        "net_profit": net_profit,
        "real_roi": round(net_profit / ad_spend, 2) if ad_spend else 0,
        "ad_profit_margin": round(net_profit / revenue * 100, 1) if revenue else 0,
        "gross_margin": round((revenue - cogs) / revenue * 100, 1) if revenue else 0,
        "_meta": {"source": "mock", "period": period},
    })


# ─────────────────────────── 預算燒爆即時監控 + 規則引擎（P0） ───────────────────────────

@app.get("/api/v1/budget/pacing")
def get_budget_pacing(
    source: str = Query("auto", pattern="^(auto|mock|real)$"),
    shop: str | None = None,
):
    """每個帳號當下花費 vs 預算 + 預測燒完時間"""
    use_real = source == "real" or (source == "auto" and ad_data_store.has_real_data())
    if use_real and ad_data_store.has_real_data():
        accounts = get_accounts()["data"]["items"]
        items = ad_data_store.aggregate_budget_pacing(shop=shop, accounts=accounts)
        if items:
            return ok({"items": items,
                       "checked_at": datetime.now(timezone.utc).isoformat(),
                       "_meta": {"source": "real"}})

    now_hour = datetime.now(timezone.utc).hour
    items = []
    for a in get_accounts()["data"]["items"]:
        daily_budget = a["spend"] / 7  # mock：用週花費除 7
        burn_today = daily_budget * (now_hour / 24) * random.uniform(0.7, 1.4)
        burn_rate = burn_today / daily_budget if daily_budget else 0
        if burn_rate > 0:
            predicted_depletion_hour = min(24, now_hour / burn_rate)
        else:
            predicted_depletion_hour = 24
        status = "ok"
        if burn_rate > 1.2: status = "danger"
        elif burn_rate > 0.9 and now_hour < 18: status = "warn"
        items.append({
            "shop_id": a["id"], "shop_name": a["name"],
            "daily_budget": round(daily_budget),
            "burn_today": round(burn_today),
            "burn_rate_pct": round(burn_rate * 100),
            "predicted_depletion_hour": round(predicted_depletion_hour, 1),
            "status": status,
        })
    return ok({"items": items, "checked_at": datetime.now(timezone.utc).isoformat(),
               "_meta": {"source": "mock"}})


@app.get("/api/v1/budget/alerts")
def get_budget_alerts():
    """目前進行中的警示"""
    pacing = get_budget_pacing()["data"]["items"]
    alerts = []
    for p in pacing:
        if p["status"] == "danger":
            alerts.append({"level": "critical", "shop": p["shop_name"],
                           "msg": f"預算將於 {p['predicted_depletion_hour']}:00 燒完，下午晚上將無曝光",
                           "burn_rate": p["burn_rate_pct"]})
        elif p["status"] == "warn":
            alerts.append({"level": "warn", "shop": p["shop_name"],
                           "msg": f"花費較預期快 {p['burn_rate_pct']}%，建議降低出價",
                           "burn_rate": p["burn_rate_pct"]})
    return ok({"items": alerts, "total": len(alerts)})


RULES_FILE = ROOT / "rules.json"


def _load_rules():
    if not RULES_FILE.exists():
        return []
    return json.loads(RULES_FILE.read_text(encoding="utf-8"))


def _save_rules(items):
    RULES_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


class RuleReq(BaseModel):
    name: str
    metric: str           # roas / ctr / cpc / spend
    operator: str         # lt / gt / lte / gte
    threshold: float
    window_days: int = 3
    action: str           # pause / lower_bid / raise_bid / notify
    action_value: float | None = None  # 出價變化幅度等
    enabled: bool = True
    scope: str = "all"    # all / specific shop / specific category


@app.get("/api/v1/rules")
def list_rules():
    return ok({"items": _load_rules()})


@app.post("/api/v1/rules")
def add_rule(req: RuleReq):
    rules = _load_rules()
    item = {"id": f"R-{int(time.time()*1000)}", **req.model_dump(),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "last_triggered": None, "trigger_count": 0}
    rules.append(item)
    _save_rules(rules)
    return ok(item)


@app.delete("/api/v1/rules/{rid}")
def delete_rule(rid: str):
    rules = [r for r in _load_rules() if r["id"] != rid]
    _save_rules(rules)
    return ok({"removed": rid})


@app.post("/api/v1/rules/run")
def run_rules_now(shop: str | None = None, dry_run: bool = False):
    """執行所有 enabled 規則 → 用真實 CSV 資料評估，回傳實際觸發紀錄與影響商品。
    dry_run=True 只看會觸發什麼，不更新 trigger_count
    """
    rules = _load_rules()
    if not ad_data_store.has_real_data():
        # 沒真實資料時 fallback 舊行為（mock）
        now = datetime.now(timezone.utc).isoformat()
        triggered = []
        for r in rules:
            if not r.get("enabled"): continue
            if random.random() < 0.5:
                r["last_triggered"] = now
                r["trigger_count"] = r.get("trigger_count", 0) + 1
                triggered.append({"rule": r["name"], "action": r["action"],
                                  "affected_count": random.randint(1, 5),
                                  "matched_products": []})
        _save_rules(rules)
        return ok({"triggered": triggered, "ran_at": now,
                   "_meta": {"source": "mock", "reason": "no_csv_data"}})

    report = rules_engine.apply_rules(rules, dry_run=dry_run, shop=shop)
    if not dry_run:
        _save_rules(rules)  # 規則中的 last_triggered/trigger_count 已被 apply_rules 修改
    return ok({
        "triggered": [
            {
                "rule_id": t.rule_id,
                "rule": t.rule_name,
                "metric": t.metric, "operator": t.operator, "threshold": t.threshold,
                "action": t.action, "action_value": t.action_value,
                "affected_count": len(t.matched_products),
                "matched_products": t.matched_products,
            }
            for t in report.triggered
        ],
        "ran_at": report.ran_at,
        "dry_run": report.dry_run,
        "total_actions": report.total_actions,
        "_meta": {"source": "real", "shop": shop},
    })


@app.post("/api/v1/rules/preview")
def preview_rules(shop: str | None = None):
    """Dry-run：列出所有 enabled 規則，看每條會命中什麼（不更新 trigger_count）"""
    rules = _load_rules()
    if not ad_data_store.has_real_data():
        return ok({"results": [], "_meta": {"source": "mock", "reason": "no_csv_data"}})
    results = rules_engine.evaluate_rules(rules, shop=shop)
    return ok({
        "results": [
            {
                "rule_id": r.rule_id, "rule_name": r.rule_name,
                "metric": r.metric, "operator": r.operator, "threshold": r.threshold,
                "action": r.action, "action_value": r.action_value,
                "evaluated_count": r.evaluated_count,
                "matched_count": len(r.matched_products),
                "matched_products": r.matched_products[:10],
            }
            for r in results
        ],
        "_meta": {"source": "real", "shop": shop},
    })


# ─────────────────────────── 商品健康度評分（P1） ───────────────────────────

def _calc_health_factors(p: dict) -> None:
    """就地計算 health_score / recommendation / health_factors"""
    rating_score = (p.get("rating", 0) / 5) * 40
    stock_score = min(15, (p.get("stock", 0) / 100) * 15)
    price_score = (p.get("price_competitiveness", 0) / 100) * 20
    sales_score = min(15, (p.get("sales_30d", 0) / 200) * 15)
    review_score = min(10, (p.get("reviews", 0) / 300) * 10)
    total = round(rating_score + stock_score + price_score + sales_score + review_score, 1)
    p["health_score"] = total
    p["recommendation"] = "強投" if total >= 70 else "正常" if total >= 50 else "減投" if total >= 30 else "停投"
    p["health_factors"] = {
        "評分": round(rating_score, 1), "庫存": round(stock_score, 1),
        "價競力": round(price_score, 1), "銷量": round(sales_score, 1),
        "評論數": round(review_score, 1),
    }


@app.get("/api/v1/products/health-scores")
def get_health_scores(
    source: str = Query("auto", pattern="^(auto|mock|real)$"),
    shop: str | None = None,
):
    """商品健康度：評分 + 庫存 + 價格競爭力 + 評論數
    source=real：用 CSV 真實商品的近 30 天 orders 當 sales_30d；評分/庫存/價競力暫時用估算值（未來接 Shopee Open API）
    """
    use_real = source == "real" or (source == "auto" and ad_data_store.has_real_data())
    if use_real and ad_data_store.has_real_data():
        sales = ad_data_store.aggregate_product_sales(shop=shop, days=30)
        if sales:
            products = []
            for pid, m in sales.items():
                # 估算評分/庫存/價競力（未接 Open API 前的假設值，根據實際銷售推估）
                # 銷量好的商品通常評分高、價競力強
                est_rating = min(5.0, 3.5 + min(1.5, m["orders"] / 50))
                est_reviews = max(10, int(m["orders"] * 1.2))
                est_stock = max(0, 200 - m["units_sold"]) if m["units_sold"] else 100
                est_price_comp = min(95, 50 + min(45, m["orders"] / 5))
                products.append({
                    "id": pid,
                    "name": m["product_name"],
                    "shop": m["shop"],
                    "rating": round(est_rating, 1),
                    "reviews": est_reviews,
                    "stock": est_stock,
                    "price_competitiveness": round(est_price_comp),
                    "sales_30d": m["orders"],
                    # 真實廣告指標
                    "ad_spend_30d": round(m["spend"], 2),
                    "ad_revenue_30d": round(m["revenue"], 2),
                    "ad_roas": round(m["revenue"] / m["spend"], 2) if m["spend"] else 0,
                    "ad_clicks_30d": m["clicks"],
                })
            for p in products:
                _calc_health_factors(p)
            products.sort(key=lambda x: x["health_score"], reverse=True)
            return ok({"items": products, "_meta": {
                "source": "real", "row_count_basis": "近 30 天 CSV 資料",
                "estimated_fields": ["rating", "reviews", "stock", "price_competitiveness"],
                "products": len(products),
            }})

    products = [
        {"id":"P1","name":"寶寶副食品調理組","shop":"D 母嬰","rating":4.7,"reviews":328,"stock":120,"price_competitiveness":85,"sales_30d":210},
        {"id":"P2","name":"快充充電器 65W","shop":"自家3C","rating":4.5,"reviews":215,"stock":80,"price_competitiveness":78,"sales_30d":155},
        {"id":"P3","name":"登山輕量背包","shop":"E 戶外","rating":4.8,"reviews":189,"stock":45,"price_competitiveness":92,"sales_30d":92},
        {"id":"P4","name":"即食料理包 8 入","shop":"A 食品行","rating":4.3,"reviews":520,"stock":300,"price_competitiveness":70,"sales_30d":280},
        {"id":"P5","name":"保濕精華液 50ml","shop":"自家美妝","rating":4.6,"reviews":412,"stock":150,"price_competitiveness":75,"sales_30d":188},
        {"id":"H1","name":"夏季短T男","shop":"B 服飾批發","rating":3.2,"reviews":48,"stock":12,"price_competitiveness":55,"sales_30d":18},
        {"id":"H2","name":"舊款手機殼 (停產)","shop":"自家3C","rating":3.0,"reviews":15,"stock":3,"price_competitiveness":30,"sales_30d":4},
        {"id":"H3","name":"過季泳裝","shop":"B 服飾批發","rating":2.8,"reviews":22,"stock":0,"price_competitiveness":40,"sales_30d":2},
    ]
    for p in products:
        _calc_health_factors(p)
    products.sort(key=lambda x: x["health_score"], reverse=True)
    return ok({"items": products, "_meta": {"source": "mock"}})


# ─────────────────────────── 競品深度監控（P1） ───────────────────────────

@app.get("/api/v1/competitor/new-products")
def get_competitor_new_products():
    """競品上新偵測：每天比對商品列表變化"""
    return ok({"items": [
        {"competitor":"耳機王國","product":"無線降噪耳機 Pro Max","price":2990,"listed_at":"2026-05-01","category":"3C","threat_level":"high"},
        {"competitor":"3C 達人館","product":"GaN 充電器 100W","price":1280,"listed_at":"2026-05-02","category":"3C","threat_level":"high"},
        {"competitor":"美妝實驗室","product":"煙醯胺精華 30ml","price":680,"listed_at":"2026-05-02","category":"美妝","threat_level":"medium"},
        {"competitor":"野營王","product":"超輕量帳篷 1人","price":3580,"listed_at":"2026-04-30","category":"戶外","threat_level":"medium"},
        {"competitor":"母嬰天地","product":"嬰兒紗布衣 5 入","price":480,"listed_at":"2026-05-03","category":"母嬰","threat_level":"low"},
    ]})


@app.get("/api/v1/competitor/price-drops")
def get_competitor_price_drops():
    """競品連續降價偵測 → 可能在發動價格戰"""
    return ok({"items": [
        {"competitor":"3C 達人館","product":"快充充電器 65W","old_price":890,"new_price":690,"drop_pct":22,"days_dropping":3,"alert":"🔴 連續 3 天降價，疑似價格戰"},
        {"competitor":"美妝實驗室","product":"保濕精華液 30ml","old_price":580,"new_price":520,"drop_pct":10,"days_dropping":2,"alert":"🟡 開始降價，建議監控"},
        {"competitor":"耳機王國","product":"藍牙耳機 Lite","old_price":1280,"new_price":980,"drop_pct":23,"days_dropping":4,"alert":"🔴 大幅降價，已影響我方排名"},
    ]})


# ─────────────────────────── 關鍵字研究（P1） ───────────────────────────

@app.get("/api/v1/keywords/explore")
def explore_keywords(seed: str = "充電器"):
    """關鍵字探索：模擬蝦皮搜尋建議 API"""
    base_volume = random.randint(15000, 50000)
    expansions = {
        "充電器": [
            {"kw":"快充充電器","volume":48000,"competition":"高","cpc":12.5,"my_rank":3,"trend":[40,42,45,46,48,48,48]},
            {"kw":"GaN 充電器","volume":22000,"competition":"中","cpc":8.8,"my_rank":None,"trend":[12,14,16,18,20,21,22]},
            {"kw":"無線充電器","volume":18000,"competition":"中","cpc":7.2,"my_rank":12,"trend":[18,18,17,17,18,18,18]},
            {"kw":"車用充電器","volume":12000,"competition":"低","cpc":4.5,"my_rank":None,"trend":[10,11,11,12,12,12,12]},
            {"kw":"iPhone 充電器","volume":35000,"competition":"高","cpc":15.0,"my_rank":7,"trend":[32,33,34,35,35,35,35]},
            {"kw":"PD 快充","volume":9800,"competition":"中","cpc":6.0,"my_rank":None,"trend":[7,8,8,9,9,10,10]},
        ],
        "保濕": [
            {"kw":"保濕精華液","volume":28000,"competition":"高","cpc":15.2,"my_rank":7,"trend":[26,27,27,28,28,28,28]},
            {"kw":"保濕面膜","volume":42000,"competition":"高","cpc":18.0,"my_rank":None,"trend":[40,41,42,42,42,42,42]},
            {"kw":"玻尿酸保濕","volume":15000,"competition":"中","cpc":9.5,"my_rank":12,"trend":[13,14,14,15,15,15,15]},
            {"kw":"乾肌保濕","volume":8200,"competition":"低","cpc":5.2,"my_rank":None,"trend":[7,7,8,8,8,8,8]},
            {"kw":"保濕乳液","volume":18000,"competition":"中","cpc":11.0,"my_rank":15,"trend":[17,17,18,18,18,18,18]},
        ],
    }
    items = expansions.get(seed, [
        {"kw":f"{seed} 推薦","volume":random.randint(10000,30000),"competition":"中","cpc":round(random.uniform(5,15),1),"my_rank":random.choice([None,5,10,15]),"trend":[random.randint(8,30) for _ in range(7)]},
        {"kw":f"{seed} 平價","volume":random.randint(5000,15000),"competition":"低","cpc":round(random.uniform(3,8),1),"my_rank":None,"trend":[random.randint(5,15) for _ in range(7)]},
        {"kw":f"高級 {seed}","volume":random.randint(3000,10000),"competition":"低","cpc":round(random.uniform(4,12),1),"my_rank":None,"trend":[random.randint(3,10) for _ in range(7)]},
    ])
    return ok({"seed": seed, "items": items})


@app.get("/api/v1/keywords/groups")
def get_keyword_groups():
    return ok({"items": [
        {"id":"G1","name":"核心詞","color":"#EE4D2D","count":12,"keywords":["快充充電器","無線耳機","保濕精華液"]},
        {"id":"G2","name":"長尾詞","color":"#16A34A","count":35,"keywords":["快充充電器 PD 65W","耳機王國同款","保濕精華液 玻尿酸"]},
        {"id":"G3","name":"品牌詞","color":"#2563EB","count":8,"keywords":["Anker","SONY","蘭蔻"]},
        {"id":"G4","name":"防禦詞","color":"#9333EA","count":5,"keywords":["[品牌] 替代品","[競品] 比較"]},
        {"id":"G5","name":"負面詞（排除）","color":"#DC2626","count":18,"keywords":["免費","破解","盜版","二手"]},
    ]})


@app.get("/api/v1/keywords/seasonal-calendar")
def get_seasonal_calendar():
    return ok({"items": [
        {"month":"5","season":"母親節","keywords":["母親節禮物","送媽媽","母親節保養"],"surge_pct":280},
        {"month":"6","season":"端午+夏季","keywords":["粽子","防曬","涼感"],"surge_pct":150},
        {"month":"7","season":"夏季旺季","keywords":["冷氣","泳裝","海邊"],"surge_pct":200},
        {"month":"8","season":"開學季","keywords":["書包","文具","制服"],"surge_pct":180},
        {"month":"11","season":"雙11","keywords":["雙11","限時搶購","年度最低"],"surge_pct":500},
        {"month":"12","season":"聖誕+雙12","keywords":["聖誕禮物","跨年","新年"],"surge_pct":350},
    ]})


# ─────────────────────────── 受眾與素材（P1） ───────────────────────────

AUDIENCES_FILE = ROOT / "audiences.json"


def _load_audiences():
    if not AUDIENCES_FILE.exists():
        return [
            {"id":"AU1","name":"加入購物車未結帳","condition":"加購 7 天內 AND 未下單","reach":3850,"conv_rate":12.5,"created_at":"2026-04-15T10:00:00Z"},
            {"id":"AU2","name":"瀏覽商品但未加購","condition":"商品頁停留 >30s AND 未加購","reach":12400,"conv_rate":3.8,"created_at":"2026-04-20T10:00:00Z"},
            {"id":"AU3","name":"近 30 天購買者（排除）","condition":"已購買 → 從新客廣告排除","reach":8200,"conv_rate":0,"created_at":"2026-04-22T10:00:00Z"},
            {"id":"AU4","name":"VIP 老客戶","condition":"歷史訂單 ≥ 3 AND 累積 >$5000","reach":2150,"conv_rate":18.2,"created_at":"2026-04-25T10:00:00Z"},
        ]
    return json.loads(AUDIENCES_FILE.read_text(encoding="utf-8"))


def _save_audiences(items):
    AUDIENCES_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


class AudienceReq(BaseModel):
    name: str
    condition: str


@app.get("/api/v1/audiences")
def list_audiences():
    return ok({"items": _load_audiences()})


@app.post("/api/v1/audiences")
def add_audience(req: AudienceReq):
    items = _load_audiences()
    item = {"id": f"AU{int(time.time()*1000)%10000}", **req.model_dump(),
            "reach": random.randint(500, 15000), "conv_rate": round(random.uniform(2, 18), 1),
            "created_at": datetime.now(timezone.utc).isoformat()}
    items.append(item)
    _save_audiences(items)
    return ok(item)


@app.delete("/api/v1/audiences/{aid}")
def remove_audience(aid: str):
    items = [a for a in _load_audiences() if a["id"] != aid]
    _save_audiences(items)
    return ok({"removed": aid})


@app.post("/api/v1/creative/generate-copy")
def generate_copy(body: dict):
    """AI 廣告文案生成（mock — 真實接 Claude API 或 OpenAI）"""
    product = body.get("product", "商品")
    style = body.get("style", "促銷")
    templates = {
        "促銷": [
            f"🔥 限時下殺！{product} 全網最低價，買到賺到！",
            f"💥 {product} 母親節優惠｜下單再送神秘小禮",
            f"⏰ 24 小時限時搶購｜{product} 第二件 5 折",
        ],
        "專業": [
            f"{product} ｜專業認證 · 品質保證 · 7 天試用",
            f"工程師推薦｜{product} 完整規格細節說明",
            f"嚴選好物 · {product} 評分 4.8 顆星 · 千則好評",
        ],
        "情感": [
            f"給最愛的人｜{product} 是最貼心的選擇",
            f"那一刻，{product} 陪你度過｜溫暖回憶",
            f"用心的禮物｜{product} 傳遞你的心意",
        ],
    }
    return ok({"copies": templates.get(style, templates["促銷"]),
               "style": style, "product": product,
               "ai_model": "claude-haiku-4.5 (mock)"})


# ─────────────────────────── 客戶 CRM + 月報（P0+P1） ───────────────────────────

CUSTOMERS_FILE = ROOT / "customers.json"


def _load_customers():
    if not CUSTOMERS_FILE.exists():
        return [
            {"id":"CU1","name":"王老闆 (A 食品行)","contact":"0912-345-678","email":"a-food@example.com","line_id":"a_food_boss",
             "shop_ids":["C001"],"contract_start":"2026-01-15","contract_end":"2027-01-14",
             "monthly_fee":35000,"target_roas":4.0,"target_acos":25,"settle_day":5,"status":"active"},
            {"id":"CU2","name":"林老闆 (B 服飾批發)","contact":"0923-456-789","email":"b-fashion@example.com","line_id":"b_fashion",
             "shop_ids":["C002"],"contract_start":"2026-03-01","contract_end":"2026-08-31",
             "monthly_fee":25000,"target_roas":3.0,"target_acos":33,"settle_day":10,"status":"warning"},
            {"id":"CU3","name":"陳董 (C 寵物用品)","contact":"0934-567-890","email":"c-pet@example.com","line_id":"c_pet_boss",
             "shop_ids":["C003"],"contract_start":"2025-09-01","contract_end":"2026-08-31",
             "monthly_fee":40000,"target_roas":3.5,"target_acos":28,"settle_day":15,"status":"active"},
            {"id":"CU4","name":"張小姐 (D 母嬰)","contact":"0945-678-901","email":"d-baby@example.com","line_id":"d_baby",
             "shop_ids":["C004"],"contract_start":"2026-02-01","contract_end":"2027-01-31",
             "monthly_fee":30000,"target_roas":3.8,"target_acos":26,"settle_day":20,"status":"active"},
            {"id":"CU5","name":"李大哥 (E 戶外)","contact":"0956-789-012","email":"e-outdoor@example.com","line_id":"e_outdoor",
             "shop_ids":["C005"],"contract_start":"2025-12-01","contract_end":"2026-11-30",
             "monthly_fee":28000,"target_roas":5.0,"target_acos":20,"settle_day":25,"status":"active"},
        ]
    return json.loads(CUSTOMERS_FILE.read_text(encoding="utf-8"))


def _save_customers(items):
    CUSTOMERS_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


@app.get("/api/v1/customers")
def list_customers():
    return ok({"items": _load_customers()})


class CustomerReq(BaseModel):
    name: str
    contact: str = ""
    email: str = ""
    line_id: str = ""
    monthly_fee: float = 0
    target_roas: float = 3.0
    target_acos: float = 30
    contract_start: str = ""
    contract_end: str = ""


@app.post("/api/v1/customers")
def add_customer(req: CustomerReq):
    items = _load_customers()
    item = {"id": f"CU{int(time.time()*1000)%10000}", **req.model_dump(),
            "shop_ids": [], "settle_day": 25, "status": "active"}
    items.append(item)
    _save_customers(items)
    return ok(item)


@app.delete("/api/v1/customers/{cid}")
def remove_customer(cid: str):
    items = [c for c in _load_customers() if c["id"] != cid]
    _save_customers(items)
    return ok({"removed": cid})


@app.get("/api/v1/reports/monthly/{customer_id}")
def get_monthly_report(
    customer_id: str,
    month: str | None = None,
    source: str = Query("auto", pattern="^(auto|mock|real)$"),
):
    """產生客戶月報資料（前端用此資料 render PDF）

    real 模式：把客戶 shop_ids 對應到 ad_reports.json 的 rows，做：
      - 整體 KPI（spend / revenue / roas / orders / ctr）
      - 真實獲利（呼叫 ad_data_store.aggregate_profit）
      - Top 3 商品（按營收）
      - 每日趨勢
      - highlights / next_month_plan 自動依數據生成
    """
    cust = next((c for c in _load_customers() if c["id"] == customer_id), None)
    if not cust:
        raise HTTPException(404, "customer not found")
    month = month or datetime.now(timezone.utc).strftime("%Y-%m")

    # 判斷要不要用 real
    shop_ids = cust.get("shop_ids", []) or []
    use_real = source == "real" or (source == "auto" and ad_data_store.has_real_data())
    has_data_for_customer = use_real and any(
        sh in ad_data_store.list_shops_with_data() for sh in shop_ids
    )

    if has_data_for_customer:
        # 對每個 shop 聚合 → 加總（month 期間）
        kpi_total = {"spend": 0, "revenue": 0, "orders": 0,
                     "clicks": 0, "impressions": 0, "row_count": 0}
        profit_total = {"revenue": 0, "ad_spend": 0, "cogs": 0,
                        "shipping_total": 0, "platform_fee": 0,
                        "payment_fee": 0, "net_profit": 0}
        all_products = {}
        all_trend = {}
        profit_cfg = _load_profit_cfg()

        for sh in shop_ids:
            kpi = ad_data_store.aggregate_kpi(shop=sh, period="month")
            kpi_total["spend"] += kpi["spend"]
            kpi_total["revenue"] += kpi["revenue"]
            kpi_total["row_count"] += kpi["_meta"]["row_count"]

            profit = ad_data_store.aggregate_profit(
                shop=sh, period="month", profit_config=profit_cfg)
            for k in ("revenue", "ad_spend", "cogs", "shipping_total",
                      "platform_fee", "payment_fee", "net_profit"):
                profit_total[k] += profit[k]

            for p in ad_data_store.aggregate_products(shop=sh, period="month", limit=100):
                pid = p["product_id"]
                if pid not in all_products:
                    all_products[pid] = p
                else:
                    # 同 product 出現在多 shop，合併
                    for k in ("spend", "revenue", "clicks", "impressions", "orders"):
                        all_products[pid][k] += p[k]

            for pt in ad_data_store.aggregate_daily_trend(shop=sh, days=30):
                d = pt["date"]
                acc = all_trend.setdefault(d, {"date": d, "spend": 0, "revenue": 0})
                acc["spend"] += pt["spend"]
                acc["revenue"] += pt["revenue"]

        roas = round(kpi_total["revenue"] / kpi_total["spend"], 2) if kpi_total["spend"] else 0
        # orders / clicks / impressions 直接 sum 自 store
        for sh in shop_ids:
            for r in ad_data_store._load()["rows"]:
                if r.get("shop") != sh:
                    continue
                kpi_total["orders"] += r.get("orders") or 0
                kpi_total["clicks"] += r.get("clicks") or 0
                kpi_total["impressions"] += r.get("impressions") or 0
        ctr_pct = round(kpi_total["clicks"] / kpi_total["impressions"] * 100, 2) if kpi_total["impressions"] else 0

        top_products = sorted(all_products.values(), key=lambda x: x["revenue"], reverse=True)[:3]
        trend = sorted(all_trend.values(), key=lambda x: x["date"])

        # 自動 highlights：根據實際數據
        target_roas = cust.get("target_roas", 3.0)
        highlights = [
            f"本月實際 ROAS {roas}（目標 {target_roas}）— "
            + ("✅ 達標" if roas >= target_roas else "❌ 未達標"),
            f"本月廣告花費 NT${kpi_total['spend']:,}、廣告營收 NT${kpi_total['revenue']:,}",
            f"扣完所有成本後淨利 NT${profit_total['net_profit']:,}（真實 ROI "
            f"{round(profit_total['net_profit']/profit_total['ad_spend'],2) if profit_total['ad_spend'] else 0}x）",
            f"涵蓋 {len(all_products)} 個商品、{kpi_total['orders']} 筆訂單、{kpi_total['row_count']} 筆 CSV 報表資料",
        ]
        if top_products:
            highlights.append(
                f"營收冠軍：{top_products[0]['product_name']}（NT${top_products[0]['revenue']:,.0f} / ROAS {top_products[0]['roas']}）"
            )

        # 下月計畫：優先用 AI 顧問動態生成（有 API key 用 Claude，沒有用規則式分析，
        # 兩者都比固定模板貼近真實數據）
        ai_source = "rule_template"
        next_month_plan = []
        try:
            ai = ai_advisor.generate_insights({
                "kpi": {"spend": kpi_total["spend"], "revenue": kpi_total["revenue"],
                        "roas": roas, "target_roas": target_roas,
                        "orders": kpi_total["orders"]},
                "profit": profit_total,
                "top_products": top_products,
                "triggered_rules": [],
            }, customer=cust)
            ai_plan = (ai.get("immediate_actions") or []) + (ai.get("this_week_plan") or [])
            ai_plan = [x for x in ai_plan if x and "無緊急" not in x]
            if ai_plan:
                next_month_plan = ai_plan[:5]
            # AI 診斷補進亮點（數據事實 + AI 洞察）
            ai_diag = [d for d in (ai.get("diagnosis") or []) if d and "符合或超越" not in d]
            if ai_diag:
                highlights = highlights[:4] + ai_diag[:2]
            ai_source = (ai.get("_meta") or {}).get("source", "rule_based")
        except Exception:
            pass

        # AI 完全沒產出時，退回原本的規則式
        if not next_month_plan:
            if roas < target_roas:
                next_month_plan.append("ROAS 未達標，下月優先優化最差 3 商品的關鍵字與出價")
            if profit_total["net_profit"] < 0:
                next_month_plan.append("⚠️ 整體淨利為負，需重新檢視商品成本結構與運費策略")
            if top_products and top_products[0]["roas"] >= target_roas * 1.2:
                next_month_plan.append(f"主推商品「{top_products[0]['product_name']}」表現亮眼，下月加碼預算")
            if not next_month_plan:
                next_month_plan = ["維持當前策略", "微調出價優化低 ROAS 商品", "測試 3 組新關鍵字擴量"]

        return ok({
            "customer": cust, "month": month,
            "metrics": {
                "revenue": kpi_total["revenue"], "spend": kpi_total["spend"],
                "roas": roas,
                "orders": kpi_total["orders"],
                "clicks": kpi_total["clicks"],
                "impressions": kpi_total["impressions"],
                "ctr": ctr_pct,
                "target_roas": target_roas,
                "kpi_achieved": roas >= target_roas,
            },
            "profit": profit_total,
            "top_products": top_products,
            "daily_trend": trend,
            "highlights": highlights,
            "next_month_plan": next_month_plan,
            "_meta": {"source": "real", "shop_ids": shop_ids,
                      "has_data_for_shops": [s for s in shop_ids if s in ad_data_store.list_shops_with_data()]},
        })

    # ── Mock fallback（原行為） ─────────────────────────
    revenue = random.randint(800000, 2500000)
    spend = random.randint(150000, 500000)
    roas = round(revenue / spend, 2)
    return ok({
        "customer": cust, "month": month,
        "metrics": {
            "revenue": revenue, "spend": spend, "roas": roas,
            "orders": random.randint(800, 3000),
            "ctr": round(random.uniform(2, 4), 1),
            "target_roas": cust["target_roas"],
            "kpi_achieved": roas >= cust["target_roas"],
        },
        "highlights": [
            f"本月 ROAS {roas}（目標 {cust['target_roas']}）",
            f"投放 {random.randint(15, 35)} 個廣告組合",
            f"優化 {random.randint(8, 20)} 次出價/預算調整",
            f"產生 {random.randint(3, 8)} 筆 AI 建議",
        ],
        "next_month_plan": [
            "預計提前 D-7 加碼母親節活動廣告",
            "新增 5 組長尾關鍵字測試",
            "停投 3 個低健康度商品釋出預算",
        ],
        "_meta": {"source": "mock", "shop_ids": shop_ids,
                  "reason": "no_csv_data" if not ad_data_store.has_real_data()
                            else "no_data_for_customer_shops"},
    })


REPORT_LOG_FILE = ROOT / "report_log.json"


@app.get("/api/v1/email/status")
def email_status():
    """前端用：判斷 SendGrid 是否已設定好"""
    return ok(email_client.config_status())


@app.post("/api/v1/reports/send")
def send_monthly_report(body: dict):
    """寄送月報。SendGrid 未設定時自動 fallback mock。"""
    cid = body.get("customer_id")
    method = body.get("method", "email")
    override_email = body.get("to_email")  # 可選：指定收件信箱（測試用）

    log = json.loads(REPORT_LOG_FILE.read_text(encoding="utf-8")) if REPORT_LOG_FILE.exists() else []
    record = {"id": f"RPT-{int(time.time()*1000)}", "customer_id": cid, "method": method,
              "sent_at": datetime.now(timezone.utc).isoformat()}

    if method == "email":
        # 取客戶資料
        cust = next((c for c in _load_customers() if c["id"] == cid), None)
        if not cust:
            raise HTTPException(404, "customer not found")
        to_email = override_email or cust.get("email")
        if not to_email:
            raise HTTPException(400, f"客戶 {cust.get('name')} 沒有 email")

        # 取月報資料（reuse 現有 endpoint logic）
        report_resp = get_monthly_report(cid)
        report_data = report_resp["data"]
        subject = f"{cust['name']} · {report_data['month']} 月報"

        result = email_client.send_monthly_report(
            to_email=to_email, subject=subject, report_data=report_data,
        )
        record["status"] = result["status"]
        record["to_email"] = to_email
        record["subject"] = subject
        if result.get("error"):
            record["error"] = result["error"]
        if result.get("note"):
            record["note"] = result["note"]
        if result.get("message_id"):
            record["message_id"] = result["message_id"]
    else:
        record["status"] = f"sent (mock, method={method})"

    log.append(record)
    REPORT_LOG_FILE.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    return ok(record)


@app.post("/api/v1/cron/monthly-reports")
def cron_monthly_reports():
    """Cloud Scheduler 每月 1 號呼叫 → 自動產所有有資料客戶的「上月」月報並寄送。
    認證走 auth middleware 的 X-Cron-Token（不是 Basic Auth）。
    SendGrid 未設定時是 mock（只記 log），設定後自動真寄。
    """
    from datetime import date as _date
    today = _date.today()
    # 上個月（year-month 字串）
    first_this_month = today.replace(day=1)
    last_month_end = first_this_month - timedelta(days=1)
    target_month = last_month_end.strftime("%Y-%m")

    results = []
    for cust in _load_customers():
        cid = cust["id"]
        shop_ids = cust.get("shop_ids") or []
        # 跳過沒綁店家或該店家沒資料的客戶
        has_data = any(s in ad_data_store.list_shops_with_data() for s in shop_ids)
        if not has_data:
            results.append({"customer_id": cid, "name": cust.get("name"),
                             "status": "skipped", "reason": "no_csv_data"})
            continue
        to_email = cust.get("email")
        if not to_email:
            results.append({"customer_id": cid, "name": cust.get("name"),
                             "status": "skipped", "reason": "no_email"})
            continue
        try:
            report_data = get_monthly_report(cid, month=target_month)["data"]
            subject = f"{cust['name']} · {target_month} 月報"
            r = email_client.send_monthly_report(
                to_email=to_email, subject=subject, report_data=report_data)
            results.append({"customer_id": cid, "name": cust.get("name"),
                            "status": r["status"], "to_email": to_email,
                            "month": target_month})
        except Exception as e:
            results.append({"customer_id": cid, "name": cust.get("name"),
                            "status": "error", "error": f"{type(e).__name__}: {e}"})

    # 寫 log
    log = json.loads(REPORT_LOG_FILE.read_text(encoding="utf-8")) if REPORT_LOG_FILE.exists() else []
    log.append({"id": f"CRON-{int(time.time()*1000)}", "type": "monthly_batch",
                "month": target_month, "ran_at": datetime.now(timezone.utc).isoformat(),
                "results": results})
    REPORT_LOG_FILE.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")

    sent = sum(1 for r in results if r["status"] in ("sent", "mock"))
    return ok({"month": target_month, "total_customers": len(results),
               "sent_or_mock": sent, "results": results})


@app.get("/api/v1/reports/log")
def get_report_log():
    if not REPORT_LOG_FILE.exists():
        return ok({"items": []})
    items = json.loads(REPORT_LOG_FILE.read_text(encoding="utf-8"))
    return ok({"items": sorted(items, key=lambda x: x["sent_at"], reverse=True)})


# ─────────────────────────── 團隊 + LINE Notify（P0+P2） ───────────────────────────

TEAM_FILE = ROOT / "team.json"
NOTIFY_CONFIG_FILE = ROOT / "notify_config.json"


def _load_team():
    if not TEAM_FILE.exists():
        return [
            {"id":"U1","name":"操作員 Alice","email":"alice@example.com","role":"operator","assigned_customers":["CU1","CU2"],"active":True},
            {"id":"U2","name":"操作員 Bob","email":"bob@example.com","role":"operator","assigned_customers":["CU3","CU4"],"active":True},
            {"id":"U3","name":"主管 Carol","email":"carol@example.com","role":"admin","assigned_customers":[],"active":True},
        ]
    return json.loads(TEAM_FILE.read_text(encoding="utf-8"))


def _save_team(items):
    TEAM_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


class TeamMemberReq(BaseModel):
    name: str
    email: str
    role: str = "operator"  # operator / admin / viewer


@app.get("/api/v1/team")
def list_team():
    return ok({"items": _load_team()})


@app.post("/api/v1/team")
def add_team_member(req: TeamMemberReq):
    items = _load_team()
    item = {"id": f"U{int(time.time()*1000)%10000}", **req.model_dump(),
            "assigned_customers": [], "active": True}
    items.append(item)
    _save_team(items)
    return ok(item)


@app.delete("/api/v1/team/{uid}")
def remove_team_member(uid: str):
    items = [t for t in _load_team() if t["id"] != uid]
    _save_team(items)
    return ok({"removed": uid})


def _load_notify_cfg():
    if not NOTIFY_CONFIG_FILE.exists():
        return {"line_token": "", "events": {
            "roas_drop": True, "budget_burn": True,
            "competitor_drop": True, "ai_suggestion": False,
            "rule_triggered": True
        }}
    return json.loads(NOTIFY_CONFIG_FILE.read_text(encoding="utf-8"))


def _save_notify_cfg(cfg):
    NOTIFY_CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


@app.get("/api/v1/notify/config")
def get_notify_config():
    cfg = _load_notify_cfg()
    safe = dict(cfg)
    if safe.get("line_token"):
        safe["line_token"] = safe["line_token"][:8] + "..." + safe["line_token"][-4:]
    return ok(safe)


@app.put("/api/v1/notify/config")
def update_notify_config(body: dict):
    cfg = _load_notify_cfg()
    if "line_token" in body and body["line_token"] and "..." not in body["line_token"]:
        cfg["line_token"] = body["line_token"]
    if "events" in body:
        cfg["events"] = body["events"]
    _save_notify_cfg(cfg)
    return ok({"updated": True})


@app.post("/api/v1/notify/test")
def test_notify():
    """送測試訊息到 LINE Notify（如果 token 有設）"""
    cfg = _load_notify_cfg()
    if not cfg.get("line_token"):
        return ok({"sent": False, "reason": "尚未設定 LINE Notify token"})
    # 真實情境：requests.post('https://notify-api.line.me/api/notify', headers={...}, data={...})
    # mock 模式
    return ok({"sent": True, "preview": "🛒 蝦皮代操儀表板 · 測試訊息送達",
               "ts": datetime.now(timezone.utc).isoformat(), "mock": True})


# ─────────────────────────── 跨平台帳號（P2） ───────────────────────────

@app.get("/api/v1/accounts/multi-platform")
def get_multi_platform():
    return ok({"items": [
        {"shop":"自家3C旗艦店","shopee":{"spend":48200,"revenue":212000,"roas":4.40},
         "momo":{"spend":22000,"revenue":98000,"roas":4.45},
         "pchome":{"spend":18000,"revenue":62000,"roas":3.44}},
        {"shop":"自家美妝專櫃","shopee":{"spend":32100,"revenue":145800,"roas":4.54},
         "momo":{"spend":15000,"revenue":58000,"roas":3.87},"pchome":None},
        {"shop":"A 食品行","shopee":{"spend":55400,"revenue":218000,"roas":3.93},
         "momo":None,"pchome":{"spend":12000,"revenue":35000,"roas":2.92}},
    ]})


# ─────────────────────────── 靜態檔 / 首頁 ───────────────────────────

if (ROOT / "static").exists():
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC_ROOT / "demo.html")


@app.get("/api.js")
def api_js():
    return FileResponse(STATIC_ROOT / "api.js", media_type="application/javascript")


# ─────────────────────────── 啟動 ───────────────────────────

if __name__ == "__main__":
    import uvicorn
    # PORT 環境變數優先（Cloud Run 必須），預設 8765 給本機開發用
    port = int(os.environ.get("PORT", "8765"))
    # Cloud Run 需要 0.0.0.0 才能讓外部連，本機預設 127.0.0.1 比較安全
    host = os.environ.get("HOST", "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1")
    uvicorn.run(app, host=host, port=port)
