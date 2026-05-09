"""
Google Trends 公開 endpoint client（無需 API key、無需套件）
─────────────────────────
責任：抓關鍵字搜尋熱度趨勢、相關上升關鍵字。

Google Trends 沒官方 API（v0），但 trends.google.com 的內部 endpoint 是公開的：
  https://trends.google.com/trends/api/explore
  https://trends.google.com/trends/api/dailytrends

本 module 直接 HTTP GET，自動處理 Google 在 JSON 開頭塞的 `)]}'` 防 XSSI。

對外公開函式：
    explore_keyword(keyword, geo='TW') → {points: [...], related_queries: [...]}
    daily_trends(geo='TW')             → list[{title, traffic, related}]

cache 同 shopee_public_api：24 小時內同一 query 不重抓。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from paths import DATA_DIR
ROOT = DATA_DIR
CACHE_FILE = ROOT / "google_trends_cache.json"
CACHE_TTL = 6 * 3600  # Trends 每 6 小時更新一次比較合理
TIMEOUT = 10

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

_lock = Lock()
_last_at = [0.0]


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


def _cache_get(key: str) -> Any:
    c = _load_cache().get(key)
    if not c or time.time() - c.get("_at", 0) > CACHE_TTL:
        return None
    return c["data"]


def _cache_set(key: str, data: Any) -> None:
    with _lock:
        c = _load_cache()
        c[key] = {"data": data, "_at": time.time()}
        _save_cache(c)


def _http_get_text(url: str) -> str:
    elapsed = time.time() - _last_at[0]
    if elapsed < 1.0:
        time.sleep(1.0 - elapsed)
    _last_at[0] = time.time()

    req = Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
    })
    try:
        with urlopen(req, timeout=TIMEOUT) as resp:
            return resp.read().decode("utf-8")
    except HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.reason}")
    except URLError as e:
        raise RuntimeError(f"網路錯誤: {e.reason}")


def _strip_xssi(text: str) -> str:
    """Google API 在 JSON 開頭塞 )]}', 為了防 XSSI；要去掉才能 parse"""
    text = text.lstrip()
    if text.startswith(")]}'"):
        text = text[4:].lstrip()
    elif text.startswith(")]}"):
        text = text[3:].lstrip()
    return text


# ─────────────────────────── 每日熱搜 ───────────────────────────

def daily_trends(geo: str = "TW") -> list[dict]:
    """今日熱搜關鍵字（用 Google Trends RSS）"""
    key = f"daily:{geo}"
    cached = _cache_get(key)
    if cached is not None:
        return cached

    # Google Trends 2024+ 新版 endpoint：trending/rss
    url = f"https://trends.google.com/trending/rss?geo={geo}"
    xml_text = _http_get_text(url)

    out = _parse_trends_rss(xml_text)
    _cache_set(key, out)
    return out


def _parse_trends_rss(xml_text: str) -> list[dict]:
    """解析 Google Trends RSS（含 ht:* 命名空間）"""
    import re
    items_xml = re.findall(r"<item>(.*?)</item>", xml_text, re.DOTALL)
    out = []
    for item_xml in items_xml:
        def _grab(tag, default=""):
            m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", item_xml, re.DOTALL)
            if not m:
                return default
            v = m.group(1)
            # 清 CDATA
            cdata = re.match(r"<!\[CDATA\[(.*?)\]\]>", v.strip(), re.DOTALL)
            return cdata.group(1) if cdata else v.strip()

        title = _grab("title")
        traffic = _grab("ht:approx_traffic", "")
        # 抓相關關鍵字（ht:news_item_title）
        news_items = re.findall(r"<ht:news_item>(.*?)</ht:news_item>",
                                 item_xml, re.DOTALL)
        articles = []
        for ni in news_items[:3]:
            ni_t = re.search(r"<ht:news_item_title[^>]*>(.*?)</ht:news_item_title>", ni, re.DOTALL)
            ni_s = re.search(r"<ht:news_item_source[^>]*>(.*?)</ht:news_item_source>", ni, re.DOTALL)
            ni_u = re.search(r"<ht:news_item_url[^>]*>(.*?)</ht:news_item_url>", ni, re.DOTALL)
            def _clean(m):
                if not m: return ""
                v = m.group(1).strip()
                c = re.match(r"<!\[CDATA\[(.*?)\]\]>", v, re.DOTALL)
                return (c.group(1) if c else v).strip()
            articles.append({
                "title": _clean(ni_t),
                "source": _clean(ni_s),
                "url": _clean(ni_u),
            })
        out.append({
            "title": title,
            "traffic": traffic,
            "related": [],  # RSS 沒這欄位
            "articles": articles,
        })
    return out


# ─────────────────────────── 關鍵字探索 ───────────────────────────

def explore_keyword(keyword: str, geo: str = "TW") -> dict:
    """探索關鍵字 — 回傳近 12 個月趨勢 + 相關上升查詢。

    Google Trends 的 explore 流程要兩步：
    1. POST /trends/api/explore 拿 widgets（含 token）
    2. 用 token 拿時間序列 + related queries

    為了簡化，這裡只用簡化版（dailytrends 的 related 已經夠用作為「相關詞」展示）。
    若要正式用，建議裝 pytrends：pip install pytrends
    """
    if not keyword.strip():
        return {"keyword": keyword, "points": [], "related": [],
                "_meta": {"source": "empty"}}

    # 用 pytrends（如果有裝）— 拿到完整時序資料
    try:
        from pytrends.request import TrendReq
        pytrends = TrendReq(hl="zh-TW", tz=-480)
        pytrends.build_payload([keyword], cat=0, timeframe="today 12-m", geo=geo)
        df = pytrends.interest_over_time()
        if df.empty:
            return {"keyword": keyword, "points": [], "related": [],
                    "_meta": {"source": "pytrends_empty"}}
        points = [{"date": d.strftime("%Y-%m-%d"), "value": int(v)}
                  for d, v in df[keyword].items()]
        related_q = pytrends.related_queries().get(keyword) or {}
        rising = [{"keyword": r["query"], "growth": r.get("value")}
                  for r in (related_q.get("rising").to_dict("records")
                            if related_q.get("rising") is not None else [])][:10]
        top = [{"keyword": r["query"], "score": r.get("value")}
               for r in (related_q.get("top").to_dict("records")
                         if related_q.get("top") is not None else [])][:10]
        return {
            "keyword": keyword,
            "points": points,
            "related_rising": rising,
            "related_top": top,
            "_meta": {"source": "pytrends", "geo": geo},
        }
    except ImportError:
        pass
    except Exception as e:
        # pytrends 報錯 → fallback 到簡化版
        return {
            "keyword": keyword, "points": [], "related": [],
            "_meta": {"source": "pytrends_error", "error": str(e),
                      "note": "pytrends 失敗，請改用 Google Trends 網站手動查或 pip install pytrends 升級"},
        }

    # pytrends 沒裝：回基本訊息
    return {
        "keyword": keyword, "points": [], "related": [],
        "_meta": {"source": "no_library",
                  "note": "需要 `pip install pytrends` 才能拿關鍵字熱度趨勢"},
    }


def is_available() -> dict:
    try:
        import pytrends as _
        has_pt = True
    except ImportError:
        has_pt = False
    return {
        "pytrends": has_pt,
        "daily_trends": True,  # 直接 HTTP，永遠可用
        "note": ("✓ pytrends 已裝（完整功能）"
                 if has_pt
                 else "⚠ pytrends 未裝；daily_trends 仍可用，但 explore_keyword 受限。建議：pip install pytrends"),
    }
