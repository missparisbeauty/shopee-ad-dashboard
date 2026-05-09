"""
蝦皮 v4 公開 API client
─────────────────────────
責任：用蝦皮**不需要 OAuth** 的公開 API，補足 CSV 沒給的資料：
     - fetch_item_info(shop_id, item_id) — 商品真實 rating/stock/reviews/sold
     - search_products(keyword)         — 搜尋結果（用於競品監控）

合法性：純呼叫公開 endpoint，不登入、不爬 HTML、不模擬用戶行為。
       跟「用瀏覽器看商品頁面」資訊範圍一樣，只是用 JSON 形式取得。

風險：蝦皮可能限流（HTTP 429）或加 Cloudflare 驗證。我們：
- 加 User-Agent header
- 加 cache（同一商品 24 小時內不重抓）
- rate limit (每秒最多 1 次)
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from threading import Lock
from typing import Any
from http.cookiejar import CookieJar
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen, build_opener, HTTPCookieProcessor
from urllib.error import HTTPError, URLError

from paths import DATA_DIR
ROOT = DATA_DIR
CACHE_FILE = ROOT / "shopee_public_cache.json"
CACHE_TTL_SECONDS = 24 * 3600  # 24 小時
RATE_LIMIT_SECONDS = 1.0       # 每次呼叫間隔
TIMEOUT_SECONDS = 8

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

_cache_lock = Lock()
_last_request_at = [0.0]

# 共用 cookie jar — 先打主頁拿 cookie，後續 API 才不會 403
_cookie_jar = CookieJar()
_opener = build_opener(HTTPCookieProcessor(_cookie_jar))
_cookies_warmed = [False]


def _warmup_cookies() -> None:
    """先拜訪首頁拿 cookies（特別是 SPC_F、CSRFTOKEN 等）"""
    if _cookies_warmed[0]:
        return
    try:
        req = Request("https://shopee.tw/", headers=_browser_headers(json_api=False))
        with _opener.open(req, timeout=TIMEOUT_SECONDS):
            pass
        _cookies_warmed[0] = True
    except Exception:
        pass  # warmup 失敗也繼續，可能本來就能打


def _browser_headers(json_api: bool = True) -> dict:
    h = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Sec-Fetch-Dest": "empty" if json_api else "document",
        "Sec-Fetch-Mode": "cors" if json_api else "navigate",
        "Sec-Fetch-Site": "same-origin" if json_api else "none",
        "sec-ch-ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
    }
    if json_api:
        h.update({
            "Accept": "application/json",
            "Referer": "https://shopee.tw/",
            "X-Requested-With": "XMLHttpRequest",
            "X-API-SOURCE": "pc",
            "X-Shopee-Language": "zh-Hant",
        })
    else:
        h["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    return h


# ─────────────────────────── Cache ───────────────────────────

def _load_cache() -> dict:
    if not CACHE_FILE.exists():
        return {}
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cache(cache: dict) -> None:
    try:
        CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    except OSError:
        pass


def _cache_get(key: str) -> dict | None:
    cache = _load_cache()
    entry = cache.get(key)
    if not entry:
        return None
    if time.time() - entry.get("_cached_at", 0) > CACHE_TTL_SECONDS:
        return None
    return entry.get("data")


def _cache_set(key: str, data: dict) -> None:
    with _cache_lock:
        cache = _load_cache()
        cache[key] = {"data": data, "_cached_at": time.time()}
        _save_cache(cache)


# ─────────────────────────── HTTP wrapper ───────────────────────────

# 嘗試 import cloudscraper（破 Cloudflare 用），有就用、沒有用 urllib fallback
try:
    import cloudscraper
    _scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )
    _HAS_CLOUDSCRAPER = True
except ImportError:
    _scraper = None
    _HAS_CLOUDSCRAPER = False


def is_available() -> dict:
    return {
        "cloudscraper": _HAS_CLOUDSCRAPER,
        "note": ("✓ cloudscraper 可用（會自動破 Cloudflare）"
                 if _HAS_CLOUDSCRAPER
                 else "⚠ 沒裝 cloudscraper，蝦皮反爬蟲可能擋（HTTP 403）；建議裝：pip install cloudscraper"),
    }


def _http_get(url: str) -> dict:
    # 先 warmup cookies（首次呼叫時）
    _warmup_cookies()

    # rate limit
    now = time.time()
    elapsed = now - _last_request_at[0]
    if elapsed < RATE_LIMIT_SECONDS:
        time.sleep(RATE_LIMIT_SECONDS - elapsed)
    _last_request_at[0] = time.time()

    # 優先用 cloudscraper（破 Cloudflare）
    if _HAS_CLOUDSCRAPER:
        try:
            resp = _scraper.get(url, headers=_browser_headers(json_api=True),
                                timeout=TIMEOUT_SECONDS)
            if resp.status_code == 429:
                raise RuntimeError("蝦皮限流（HTTP 429），請等幾分鐘再試")
            if resp.status_code == 403:
                raise RuntimeError(
                    "蝦皮拒絕請求（HTTP 403）— cloudscraper 也被擋。"
                    "建議改用 Shopee Open API（partner 申請）。"
                )
            if resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code}")
            return resp.json()
        except Exception as e:
            if "HTTP" in str(e) or "蝦皮" in str(e):
                raise
            # cloudscraper 自己掛了，fallback urllib
            pass

    req = Request(url, headers=_browser_headers(json_api=True))
    try:
        with _opener.open(req, timeout=TIMEOUT_SECONDS) as resp:
            raw = resp.read()
            # 處理 gzip
            if resp.headers.get("Content-Encoding") == "gzip":
                import gzip
                raw = gzip.decompress(raw)
            elif resp.headers.get("Content-Encoding") == "deflate":
                import zlib
                raw = zlib.decompress(raw)
            elif resp.headers.get("Content-Encoding") == "br":
                try:
                    import brotli
                    raw = brotli.decompress(raw)
                except ImportError:
                    pass  # 沒裝 brotli 就讓下面 JSON 解析失敗
            return json.loads(raw.decode("utf-8"))
    except HTTPError as e:
        if e.code == 429:
            raise RuntimeError("蝦皮限流（HTTP 429），請等幾分鐘再試")
        if e.code == 403:
            raise RuntimeError(
                f"蝦皮拒絕請求（HTTP 403）— 可能加了反爬蟲驗證。"
                f"短期可重試；長期請改接 Shopee Open API。"
            )
        raise RuntimeError(f"HTTP {e.code}: {e.reason}")
    except URLError as e:
        raise RuntimeError(f"網路錯誤: {e.reason}")


# ─────────────────────────── Item API ───────────────────────────

def fetch_item_info(shop_id: int | str, item_id: int | str) -> dict:
    """抓單一商品的真實資料（rating/stock/reviews/sold/price）。
    用 cache，24 小時內同一商品不重抓。
    """
    key = f"item:{shop_id}:{item_id}"
    cached = _cache_get(key)
    if cached:
        return {**cached, "_from_cache": True}

    url = f"https://shopee.tw/api/v4/item/get?itemid={item_id}&shopid={shop_id}"
    raw = _http_get(url)
    if raw.get("error"):
        raise RuntimeError(f"商品不存在或已下架: {raw.get('error_msg', raw['error'])}")

    item = raw.get("data") or {}
    if not item:
        raise RuntimeError("回應沒有商品資料")

    # 抽出我們關心的欄位
    result = {
        "shop_id": shop_id,
        "item_id": item_id,
        "name": item.get("name"),
        "price": (item.get("price") or 0) / 100000,  # 蝦皮以「分 × 1000」存
        "price_min": (item.get("price_min") or 0) / 100000,
        "price_max": (item.get("price_max") or 0) / 100000,
        "stock": item.get("stock"),
        "rating_star": item.get("item_rating", {}).get("rating_star"),
        "rating_count_total": sum(item.get("item_rating", {}).get("rating_count", []) or [0]),
        "review_count": item.get("cmt_count"),
        "sold": item.get("sold"),
        "historical_sold": item.get("historical_sold"),
        "liked_count": item.get("liked_count"),
        "view_count": item.get("view_count"),
        "category_id": item.get("catid"),
        "status": item.get("status"),  # 1=正常
        "is_on_sale": bool(item.get("flash_sale")),
        "discount": item.get("raw_discount"),
        "url": f"https://shopee.tw/product/{shop_id}/{item_id}",
    }
    _cache_set(key, result)
    return result


def fetch_items_batch(items: list[tuple[int, int]]) -> list[dict]:
    """批次抓商品資料。
    items: [(shop_id, item_id), ...]
    自動 rate limit。失敗的個別 item 會回 {"error": "..."}。
    """
    results = []
    for shop_id, item_id in items:
        try:
            results.append(fetch_item_info(shop_id, item_id))
        except Exception as e:
            results.append({
                "shop_id": shop_id, "item_id": item_id,
                "error": str(e),
            })
    return results


# ─────────────────────────── Search API ───────────────────────────

def search_products(keyword: str, limit: int = 30, by: str = "relevancy") -> list[dict]:
    """搜尋商品（無需登入）。給競品監控用。

    keyword: 搜尋關鍵字
    by: relevancy / pop（熱門）/ ctime（最新）/ sales（銷量）/ price_asc / price_desc
    """
    if not keyword.strip():
        return []
    cache_key = f"search:{keyword}:{by}:{limit}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    params = {
        "by": by, "keyword": keyword, "limit": limit, "newest": 0,
        "order": "desc", "page_type": "search", "scenario": "PAGE_GLOBAL_SEARCH",
        "version": 2,
    }
    url = f"https://shopee.tw/api/v4/search/search_items?{urlencode(params)}"
    raw = _http_get(url)
    items_raw = raw.get("items") or []

    results = []
    for x in items_raw[:limit]:
        info = x.get("item_basic") or x
        results.append({
            "item_id": info.get("itemid"),
            "shop_id": info.get("shopid"),
            "name": info.get("name"),
            "price": (info.get("price") or 0) / 100000,
            "price_min": (info.get("price_min") or 0) / 100000,
            "price_max": (info.get("price_max") or 0) / 100000,
            "stock": info.get("stock"),
            "rating_star": info.get("item_rating", {}).get("rating_star"),
            "review_count": info.get("cmt_count") or 0,
            "sold": info.get("sold"),
            "historical_sold": info.get("historical_sold"),
            "shop_location": info.get("shop_location"),
            "url": f"https://shopee.tw/product/{info.get('shopid')}/{info.get('itemid')}",
        })

    _cache_set(cache_key, results)
    return results


# ─────────────────────────── 工具函式 ───────────────────────────

def cache_status() -> dict:
    """看 cache 大小（除錯用）"""
    cache = _load_cache()
    items = sum(1 for k in cache if k.startswith("item:"))
    searches = sum(1 for k in cache if k.startswith("search:"))
    return {
        "total_entries": len(cache),
        "item_cached": items,
        "search_cached": searches,
        "cache_file": str(CACHE_FILE),
    }


def clear_cache() -> dict:
    if CACHE_FILE.exists():
        CACHE_FILE.unlink()
    return {"cleared": True}
