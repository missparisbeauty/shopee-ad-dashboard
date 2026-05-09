"""
Shopee Open API 串接
─────────────────────────
責任：用已綁定的 access_token 呼叫蝦皮 API，補上 CSV 沒有的資料：
     - 商品列表（rating, stock, reviews） → 補商品健康度
     - 訂單明細（真實營收 vs 廣告營收差異） → 找出自然流量比例

對外公開函式：
    is_configured() → bool
    config_status() → dict
    fetch_item_list(shop_id, access_token, page_size=50)        → list[item_id]
    fetch_item_base_info(shop_id, access_token, item_ids)       → list[dict]
    fetch_order_list(shop_id, access_token, time_from, time_to) → list[order_sn]

未設定 SHOPEE_PARTNER_ID 環境變數時自動 mock，
讓開發/Demo 可以走完整流程不需要實際 partner 帳號。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import random
import time
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError

PARTNER_ID = os.environ.get("SHOPEE_PARTNER_ID", "")
PARTNER_KEY = os.environ.get("SHOPEE_PARTNER_KEY", "")
API_HOST = os.environ.get("SHOPEE_API_HOST", "https://partner.shopeemobile.com")

_MOCK_PARTNER_ID = "MOCK_PARTNER_123456"


def is_configured() -> bool:
    """有真實 partner_id（不是 mock）+ partner_key → 可呼叫真實 API"""
    return bool(PARTNER_ID) and PARTNER_ID != _MOCK_PARTNER_ID and bool(PARTNER_KEY)


def config_status() -> dict:
    return {
        "partner_id_set": bool(PARTNER_ID) and PARTNER_ID != _MOCK_PARTNER_ID,
        "partner_key_set": bool(PARTNER_KEY),
        "api_host": API_HOST,
        "ready": is_configured(),
        "mode": "real" if is_configured() else "mock",
    }


# ─────────────────────────── HMAC 簽章 ───────────────────────────

def _sign(path: str, ts: int, access_token: str = "", shop_id: str = "") -> str:
    """蝦皮 v2 API 標準簽章：HMAC-SHA256(partner_id + path + ts + token + shop_id)"""
    base = f"{PARTNER_ID}{path}{ts}{access_token}{shop_id}".encode()
    return hmac.new(PARTNER_KEY.encode(), base, hashlib.sha256).hexdigest()


def _build_url(path: str, shop_id: str, access_token: str, extra: dict | None = None) -> str:
    ts = int(time.time())
    params = {
        "partner_id": PARTNER_ID,
        "timestamp": ts,
        "sign": _sign(path, ts, access_token, shop_id),
        "shop_id": shop_id,
        "access_token": access_token,
    }
    if extra:
        params.update(extra)
    return f"{API_HOST}{path}?{urlencode(params)}"


def _http_get(url: str, timeout: float = 10) -> dict:
    req = Request(url, headers={"Accept": "application/json"})
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ─────────────────────────── Mock 資料產生 ───────────────────────────

def _mock_items(shop_id: str, count: int = 8) -> list[dict]:
    """模擬 API 回應 — 用於沒設 partner_id 時的 demo"""
    rand = random.Random(int(shop_id) if shop_id.isdigit() else hash(shop_id))
    sample_names = [
        "保濕精華液 50ml", "維他命C精華 30ml", "玻尿酸面膜 5片",
        "美白乳液 165g", "靈芝外囊泡精華 30ml", "胺基酸潔顏霜 100g",
        "防曬霜 SPF50+", "抗老晚霜 50g",
    ]
    items = []
    for i, name in enumerate(sample_names[:count]):
        items.append({
            "item_id": int(f"{shop_id[-3:] if len(shop_id)>=3 else shop_id}{1000+i}"),
            "item_name": f"[mock] {name}",
            "item_status": "NORMAL",
            "rating_star": round(rand.uniform(3.5, 5.0), 1),
            "review_count": rand.randint(20, 800),
            "stock": rand.randint(0, 300),
            "current_price": rand.choice([180, 240, 350, 480, 690, 980]),
            "category_id": 100018,
        })
    return items


# ─────────────────────────── 真實 API 包裝 ───────────────────────────

def fetch_item_list(shop_id: str, access_token: str, page_size: int = 50) -> list[dict]:
    """拿商品列表（基本欄位）。未設定 → mock。"""
    if not is_configured():
        return _mock_items(shop_id, count=min(page_size, 8))

    path = "/api/v2/product/get_item_list"
    url = _build_url(path, shop_id, access_token, extra={
        "offset": 0, "page_size": page_size, "item_status": "NORMAL",
    })
    try:
        resp = _http_get(url)
        if resp.get("error"):
            raise RuntimeError(f"shopee api error: {resp['error']} - {resp.get('message','')}")
        return resp.get("response", {}).get("item", [])
    except (HTTPError, RuntimeError) as e:
        raise RuntimeError(f"fetch_item_list 失敗: {e}")


def fetch_item_base_info(shop_id: str, access_token: str, item_ids: list[int]) -> list[dict]:
    """拿商品詳細（rating, stock, reviews 等）"""
    if not is_configured():
        # mock 模式：直接回傳 _mock_items 對應的子集
        all_mock = {i["item_id"]: i for i in _mock_items(shop_id, count=8)}
        return [all_mock[i] for i in item_ids if i in all_mock]

    path = "/api/v2/product/get_item_base_info"
    ids_str = ",".join(str(i) for i in item_ids[:50])  # API 上限 50 個
    url = _build_url(path, shop_id, access_token, extra={"item_id_list": ids_str})
    try:
        resp = _http_get(url)
        if resp.get("error"):
            raise RuntimeError(f"shopee api error: {resp['error']}")
        return resp.get("response", {}).get("item_list", [])
    except (HTTPError, RuntimeError) as e:
        raise RuntimeError(f"fetch_item_base_info 失敗: {e}")


def fetch_order_list(shop_id: str, access_token: str,
                     time_from: int, time_to: int,
                     page_size: int = 50) -> list[dict]:
    """拿訂單列表（time_from/time_to 為 unix timestamp）"""
    if not is_configured():
        # mock 模式：產 5 筆假訂單
        now = int(time.time())
        return [{
            "order_sn": f"MOCK{shop_id}{now+i}",
            "create_time": now - i * 3600,
            "order_status": random.choice(["READY_TO_SHIP", "SHIPPED", "COMPLETED"]),
            "total_amount": random.choice([280, 450, 690, 980, 1280]),
        } for i in range(5)]

    path = "/api/v2/order/get_order_list"
    url = _build_url(path, shop_id, access_token, extra={
        "time_range_field": "create_time",
        "time_from": time_from, "time_to": time_to,
        "page_size": page_size,
    })
    try:
        resp = _http_get(url)
        if resp.get("error"):
            raise RuntimeError(f"shopee api error: {resp['error']}")
        return resp.get("response", {}).get("order_list", [])
    except (HTTPError, RuntimeError) as e:
        raise RuntimeError(f"fetch_order_list 失敗: {e}")


# ─────────────────────────── 高層業務函式 ───────────────────────────

def sync_products(shop_id: str, access_token: str) -> dict:
    """完整同步商品：list → base_info → 回傳合併"""
    items = fetch_item_list(shop_id, access_token, page_size=50)
    item_ids = [i["item_id"] for i in items if "item_id" in i]
    if not item_ids:
        return {"shop_id": shop_id, "items": [], "total": 0,
                "mode": config_status()["mode"]}
    if not is_configured():
        # mock：fetch_item_list 已經回包含 rating/stock 的完整資料
        details = items
    else:
        details = fetch_item_base_info(shop_id, access_token, item_ids)
    return {
        "shop_id": shop_id,
        "items": details, "total": len(details),
        "mode": config_status()["mode"],
        "synced_at": time.time(),
    }
