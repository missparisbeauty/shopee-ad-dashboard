"""
蝦皮廣告報表 CSV 解析器
─────────────────────────
責任：把賣家中心匯出的廣告 CSV（中/英文欄位、UTF-8/Big5、千分號/百分號）
     正規化成統一 schema，供 store 持久化、儀表板聚合使用。

對外公開函式：
    detect_encoding(raw_bytes)      → str
    parse_csv(raw_bytes)            → ParseResult
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

# ─────────────────────────── 欄位中英對照表 ───────────────────────────
# key = 標準欄位名；value = 可能出現的別名（小寫、去空白後比對）
# 涵蓋蝦皮關鍵字廣告、關聯廣告、廣告搜尋、Mass Editor 匯出等格式
COLUMN_ALIASES: dict[str, list[str]] = {
    "date": [
        "日期", "date", "report date", "report_date", "stat_date", "統計日期", "資料日期",
        "報表日期", "投放日期",
        # 對於「廣告活動 lifetime 統計」報表，用「結束日期」當 fallback 代表日
        "結束日期", "end date", "end_date",
    ],
    "product_id": [
        "商品id", "商品 id", "product id", "product_id", "item id", "item_id",
        "itemid", "商品編號", "商品代碼", "sku id", "sku_id", "sku編號",
        "主商品編號", "主商品id",
    ],
    "product_name": [
        "商品名稱", "商品", "product name", "product_name", "item name", "item_name", "name", "title",
        "主商品名稱", "廣告商品", "廣告商品名稱",
    ],
    "campaign_name": [
        "廣告活動", "活動名稱", "campaign", "campaign name", "campaign_name", "ad group", "ad_group",
        "廣告名稱", "廣告組合", "廣告類型", "ad name", "ad_name", "ad type", "ad_type",
        "廣告商品名稱（廣告id）", "廣告商品名稱(廣告id)",
    ],
    "impressions": [
        "曝光數", "曝光次數", "曝光", "impressions", "impr", "impression", "views", "show",
        "累積曝光數", "累積曝光", "總曝光", "總曝光數", "曝光",
        "瀏覽數", "瀏覽次數", "商品瀏覽數",  # 蝦皮 CPC 報表用「瀏覽數」
    ],
    "clicks": [
        "點擊數", "點擊次數", "點擊", "clicks", "click",
        "累積點擊數", "累積點擊", "總點擊", "總點擊數",
        "商品點擊數",  # 蝦皮 CPC 報表的商品頁點擊數
    ],
    "ctr": [
        "點擊率", "ctr", "click rate", "click_rate", "點擊率(%)", "ctr(%)",
        "累積點擊率",
        "商品點擊率",  # 蝦皮 CPC 報表的商品頁點擊率
    ],
    "spend": [
        "花費", "廣告花費", "成本", "cost", "spend", "ad spend", "ad_spend", "費用",
        "累積花費", "累計花費", "總花費", "已使用預算",
    ],
    "orders": [
        "訂單數", "訂單", "orders", "order count", "conv", "conversions", "轉換數", "成交筆數",
        "累積訂單數", "累計訂單數", "總訂單數", "直接訂單數", "間接訂單數", "訂單筆數",
        "直接轉換數",  # 蝦皮 CPC 報表「直接轉換數」= 直接歸因訂單
    ],
    "units_sold": [
        "銷售件數", "件數", "units", "units sold", "sold", "銷售數量",
        "累積銷售件數", "累計銷售件數", "直接銷售件數", "間接銷售件數",
        "銷售數", "直接銷售數",  # 蝦皮 CPC 報表用法
    ],
    "revenue": [
        "銷售額", "營業額", "成交金額", "gmv", "sales", "revenue", "ad revenue",
        "ad_revenue", "業績", "成交金額(nt$)", "成交金額(twd)",
        "累積銷售額", "累計銷售額", "總銷售額", "直接銷售額", "間接銷售額",
        "廣告銷售金額", "投廣銷售額",
        "銷售金額", "直接銷售金額", "vouchered sales",  # 蝦皮 CPC 報表用「銷售金額」
    ],
    "roas": [
        "roas", "投報率", "回報率", "廣告投報率",
        "累積roas", "廣告投資報酬率", "投放回報",
        "投入產出比", "直接投入產出比",  # 蝦皮 CPC 報表用「投入產出比」
    ],
    "cpc": [
        "cpc", "每次點擊成本", "平均點擊成本",
        "平均cpc", "累積cpc",
        "每一筆轉換的成本", "每一筆直接轉換的成本",  # 嚴格說是 CPA，蝦皮報表常見
    ],
    "cpm": [
        "cpm", "每千次曝光成本",
    ],
    "acos": [
        "acos", "廣告銷售成本占比", "廣告佔比",
        "廣告銷售比例",
        "成本收入比率", "直接成本收入比率",  # 蝦皮 CPC 報表用「成本收入比率」
    ],
    # 蝦皮「關鍵字/版位層級數據」CSV 專用欄位
    "keyword": [
        "關鍵字", "keyword", "搜尋關鍵字", "搜尋字", "search keyword",
        "查詢字", "search query", "query", "term", "search term",
        "match keyword", "matched keyword",
    ],
    "placement": [
        "版位", "placement", "版位類型", "ad placement",
        "曝光版位", "廣告版位", "投放版位",
        # 蝦皮常見版位名稱（可能整列就是版位名，但別名比對是欄位名）
    ],
}

# 反向索引：別名（normalize 後）→ 標準名
_ALIAS_INDEX: dict[str, str] = {}
for std, aliases in COLUMN_ALIASES.items():
    for a in aliases:
        _ALIAS_INDEX[a.strip().lower().replace(" ", "")] = std
    _ALIAS_INDEX[std] = std  # 標準名本身也算


# ─────────────────────────── 結構 ───────────────────────────

@dataclass
class ParseResult:
    rows: list[dict[str, Any]]              # 正規化後的每筆紀錄
    column_map: dict[str, str]              # 原始 header → 標準欄位（unmapped 為 None）
    raw_columns: list[str]                  # 原始 header 順序
    encoding: str                           # 偵測到的編碼
    skipped_rows: int                       # 因缺資料或解析失敗而跳過的列數
    summary: dict[str, Any] = field(default_factory=dict)   # 整體統計
    metadata: dict[str, Any] = field(default_factory=dict)  # 從摘要列抽到的元資料（期間/賣場名/匯出時間）


# ─────────────────────────── 編碼偵測 ───────────────────────────

ENCODING_CANDIDATES = ("utf-8-sig", "utf-8", "big5", "cp950", "gb18030")


def detect_encoding(raw: bytes) -> str:
    """嘗試順序解碼，回傳第一個成功的編碼名稱。"""
    for enc in ENCODING_CANDIDATES:
        try:
            raw.decode(enc)
            return enc
        except UnicodeDecodeError:
            continue
    raise ValueError("無法解析檔案編碼（嘗試 utf-8 / big5 / cp950 / gb18030 皆失敗）")


# ─────────────────────────── 欄位映射 ───────────────────────────

def _normalize_header(h: str) -> str:
    return h.strip().lower().replace(" ", "").replace("　", "")


def map_columns(headers: list[str]) -> dict[str, str]:
    """原始 header → 標準欄位名（找不到的回 None）。"""
    out: dict[str, str] = {}
    for h in headers:
        key = _normalize_header(h)
        out[h] = _ALIAS_INDEX.get(key, "")
    return out


# ─────────────────────────── 數值/日期解析 ───────────────────────────

_NUM_CLEAN_RE = re.compile(r"[,\s$NT\$]")  # 去除千分號、空白、貨幣符號


def parse_number(v: Any, *, percent: bool = False) -> float | None:
    """字串/數字 → float。處理千分號、百分號、空字串、N/A。"""
    if v is None:
        return None
    s = str(v).strip()
    if not s or s in ("-", "—", "N/A", "n/a", "NaN", "null"):
        return None
    is_percent = s.endswith("%") or percent
    s = s.rstrip("%")
    s = _NUM_CLEAN_RE.sub("", s)
    try:
        n = float(s)
    except ValueError:
        return None
    if is_percent:
        n /= 100.0
    return n


def parse_int(v: Any) -> int | None:
    n = parse_number(v)
    if n is None:
        return None
    return int(round(n))


_DATE_FORMATS = (
    "%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d",
    "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S",
    "%m/%d/%Y", "%d/%m/%Y",
    "%Y%m%d",
)


def parse_date(v: Any) -> str | None:
    """各種格式 → ISO date 字串 (YYYY-MM-DD)。"""
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    # ISO date 已經是標準的快速通道
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        try:
            return date.fromisoformat(s[:10]).isoformat()
        except ValueError:
            pass
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


# ─────────────────────────── 主解析流程 ───────────────────────────

def parse_csv(raw: bytes) -> ParseResult:
    """主入口：raw bytes → 正規化結果。"""
    encoding = detect_encoding(raw)
    text = raw.decode(encoding)

    # 用 csv.Sniffer 偵測分隔符（蝦皮多為逗號，少數舊報表為 tab）
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
    except csv.Error:
        dialect = csv.excel

    reader = csv.reader(io.StringIO(text), dialect=dialect)
    rows = list(reader)
    if not rows:
        raise ValueError("CSV 是空的")

    # 蝦皮報表常在 header 之前有「所有CPC成效報告」「報表期間」等摘要列，自動跳過
    header_idx, header_score = _find_header_row(rows)
    metadata = _extract_metadata(rows, header_idx)
    fallback_date = metadata.get("report_period_end") or metadata.get("exported_at")
    raw_headers = [h.strip() for h in rows[header_idx]]
    column_map = map_columns(raw_headers)
    has_any_known = any(v for v in column_map.values())
    if not has_any_known:
        # 給診斷資訊：列出前 10 列每列的第一個 cell（讓使用者/開發者快速理解 CSV 結構）
        preview = []
        for i, r in enumerate(rows[:10]):
            first_cells = [c.strip() for c in r[:3] if c and c.strip()]
            preview.append(f"  第{i+1}列: {first_cells if first_cells else '(空白)'}")
        raise ValueError(
            f"無法辨識任何欄位（最佳掃描分數 {header_score}，至少需 3）。\n"
            f"系統選了第 {header_idx+1} 列當 header：{raw_headers[:8]}\n"
            f"CSV 前 10 列預覽：\n" + "\n".join(preview) +
            f"\n\n如果你的 CSV 是蝦皮真實報表但無法辨識，請把 header 那一列傳給開發者擴充欄位別名。"
        )

    parsed_rows: list[dict[str, Any]] = []
    skipped = 0

    for r in rows[header_idx + 1:]:
        if not any(c.strip() if isinstance(c, str) else c for c in r):
            continue  # 全空白列
        if len(r) < len(raw_headers):
            r = r + [""] * (len(raw_headers) - len(r))
        elif len(r) > len(raw_headers):
            r = r[: len(raw_headers)]

        record: dict[str, Any] = {}
        for raw_h, val in zip(raw_headers, r):
            std = column_map.get(raw_h)
            if not std:
                continue
            # 多個原始欄位對到同一個標準欄位時（如「銷售金額」「直接銷售金額」「Vouchered Sales」
            # 都對到 revenue），保留第一個有值的，避免後面 0/空字串覆蓋
            existing = record.get(std)
            if existing not in (None, "", 0, 0.0):
                continue
            if std == "date":
                parsed = parse_date(val)
                if parsed:
                    record["date"] = parsed
            elif std in ("product_id", "product_name", "campaign_name", "keyword", "placement"):
                v = str(val).strip()
                if v:
                    record[std] = v
            elif std in ("impressions", "clicks", "orders", "units_sold"):
                v = parse_int(val)
                if v is not None and v != 0:
                    record[std] = v
                elif std not in record:
                    record[std] = v  # 留 None/0 當 fallback
            elif std in ("ctr", "acos"):
                v = parse_number(val, percent=True)
                if v not in (None, 0, 0.0):
                    record[std] = v
                elif std not in record:
                    record[std] = v
            else:  # spend / revenue / roas / cpc / cpm
                v = parse_number(val)
                if v not in (None, 0, 0.0):
                    record[std] = v
                elif std not in record:
                    record[std] = v

        # 必要欄位檢查：至少要有「身份識別欄位」之一（product_id / product_name / campaign_name / keyword / placement）
        # 關鍵字/版位 CSV 沒 product_id，但有 keyword 或 placement
        if not any(record.get(k) for k in
                   ("product_id", "product_name", "campaign_name", "keyword", "placement")):
            skipped += 1
            continue
        if not any(record.get(k) is not None for k in ("impressions", "clicks", "spend", "revenue", "orders")):
            skipped += 1
            continue

        # 沒有日期但 metadata 有「期間結束日」→ 用它當代表日（蝦皮 lifetime 報表常見）
        if not record.get("date") and fallback_date:
            record["date"] = fallback_date

        # 沒有 product_name 但有 campaign_name → 用 campaign_name 當商品名
        # （蝦皮 CPC 報表沒「商品名稱」欄，但「廣告名稱」對單品廣告而言就是商品名）
        if not record.get("product_name") and record.get("campaign_name"):
            record["product_name"] = record["campaign_name"]

        # 補算 derived 欄位（CSV 沒提供時）
        _enrich_derived(record)
        parsed_rows.append(record)

    summary = _summarize(parsed_rows)
    # 把 metadata 重要欄位也放進 summary 方便前端取用
    summary["report_period_start"] = metadata.get("report_period_start")
    summary["report_period_end"] = metadata.get("report_period_end")
    summary["period_days"] = metadata.get("period_days")
    summary["is_lifetime_report"] = metadata.get("is_lifetime_report", False)
    summary["shop_name_in_csv"] = metadata.get("shop_name")
    # 偵測報表類型：總體（overall）vs 關鍵字/版位（keyword_placement）
    summary["report_type"] = detect_report_type(column_map, parsed_rows)
    return ParseResult(
        rows=parsed_rows,
        column_map=column_map,
        raw_columns=raw_headers,
        encoding=encoding,
        skipped_rows=skipped,
        summary=summary,
        metadata=metadata,
    )


# ─────────────────────────── 內部工具 ───────────────────────────

_PERIOD_RE = re.compile(r"(\d{4}[/\-\.]\d{1,2}[/\-\.]\d{1,2})\s*[-~–]\s*(\d{4}[/\-\.]\d{1,2}[/\-\.]\d{1,2})")
_DATE_INLINE_RE = re.compile(r"\d{4}[/\-\.]\d{1,2}[/\-\.]\d{1,2}")


def _extract_metadata(rows: list[list[str]], header_idx: int) -> dict:
    """從 header 之前的「摘要列」抓元資料：report_period_end / exported_at / shop_name 等。
    蝦皮報表常見格式：
        ['期間', '2026/05/03 - 2026/05/03']
        ['報表匯出時間', '2026/05/03 16:50']
        ['賣場名稱', '科爸好皮 Keba']
    """
    meta = {}
    for r in rows[:header_idx]:
        if len(r) < 2:
            continue
        key = r[0].strip().lower()
        val = r[1].strip() if len(r) > 1 else ""
        if not val:
            continue
        if "期間" in key or "report period" in key or "date range" in key:
            m = _PERIOD_RE.search(val)
            if m:
                meta["report_period_start"] = parse_date(m.group(1))
                meta["report_period_end"] = parse_date(m.group(2))
            else:
                # 單一日期
                m2 = _DATE_INLINE_RE.search(val)
                if m2:
                    meta["report_period_end"] = parse_date(m2.group(0))
                    meta["report_period_start"] = meta["report_period_end"]
        elif "匯出" in key or "exported" in key or "下載時間" in key:
            m = _DATE_INLINE_RE.search(val)
            if m:
                meta["exported_at"] = parse_date(m.group(0))
        elif "賣場" in key or "shop name" in key or "store name" in key:
            meta["shop_name"] = val
        elif "賣場id" in key.replace(" ", "") or "shop id" in key:
            meta["shop_id"] = val
        elif "使用者" in key or "user" in key:
            meta["username"] = val

    # 推算期間天數 + 是否為「lifetime / 多日彙總」報表
    start = meta.get("report_period_start")
    end = meta.get("report_period_end")
    if start and end:
        try:
            d1 = date.fromisoformat(start)
            d2 = date.fromisoformat(end)
            days = (d2 - d1).days + 1
            meta["period_days"] = days
            meta["is_lifetime_report"] = days > 1
        except (ValueError, TypeError):
            pass
    return meta


def _find_header_row(rows: list[list[str]], max_scan: int = 20) -> tuple[int, int]:
    """找第一列「至少 3 個欄位能被映射到標準名」的列當作 header。
    回傳 (header_idx, best_score)。
    """
    best_idx, best_score = 0, -1
    for idx in range(min(max_scan, len(rows))):
        cells = [h.strip() for h in rows[idx] if h and h.strip()]
        if len(cells) < 3:
            continue  # 標題列/摘要列通常只有 1-2 個 cell，跳過
        m = map_columns(cells)
        score = sum(1 for v in m.values() if v)
        if score > best_score:
            best_score, best_idx = score, idx
        if score >= 3:
            return idx, score
    return best_idx, best_score


def detect_report_type(column_map: dict[str, str], rows: list[dict[str, Any]]) -> str:
    """偵測報表類型 — 判斷是「關鍵字/版位層級」還是「廣告活動總體」報表。

    判斷邏輯：
    - 有 keyword 欄位 → 必為 keyword_placement
    - 有 placement 欄位，且 placement 值不是蝦皮「廣告設定版位」關鍵字（所有/全站/搜尋...）
      → keyword_placement（每列代表一個版位的分析數據）
    - 其餘 → overall

    蝦皮「總體報表」也有「版位」欄，但值是廣告設定（如「所有」「全站推廣」），
    不是版位層級分析，不應誤判為 keyword_placement。
    """
    # 廣告設定版位值（蝦皮「總體報表」的版位欄常見值）
    AD_SETTING_PLACEMENTS = {
        "所有", "全站", "全站推廣", "全站推廣-自訂roi", "搜尋", "推薦",
        "gmv max auto bidding (shop)", "gmv max auto bidding",
        "all", "search", "recommended",
        "-", "—", "",  # 空值/佔位符
    }

    mapped_stds = set(column_map.values())

    # 有 keyword 欄位 → 一定是關鍵字層級報表
    if "keyword" in mapped_stds:
        for r in rows:
            if r.get("keyword"):
                return "keyword_placement"

    # 有 placement 欄位 → 看值是不是「版位分析數據」還是「廣告設定描述」
    if "placement" in mapped_stds:
        for r in rows:
            val = (r.get("placement") or "").strip()
            if val and val.lower() not in AD_SETTING_PLACEMENTS:
                return "keyword_placement"  # 有非廣告設定的版位值 → 真正的版位分析

    return "overall"


def _enrich_derived(rec: dict[str, Any]) -> None:
    """若報表沒給 ROAS/CTR/CPC/ACOS，從原始指標推算。"""
    spend = rec.get("spend") or 0
    revenue = rec.get("revenue") or 0
    clicks = rec.get("clicks") or 0
    impressions = rec.get("impressions") or 0

    if rec.get("roas") is None and spend > 0:
        rec["roas"] = round(revenue / spend, 4) if revenue else 0.0
    if rec.get("ctr") is None and impressions > 0:
        rec["ctr"] = round(clicks / impressions, 6)
    if rec.get("cpc") is None and clicks > 0:
        rec["cpc"] = round(spend / clicks, 4)
    if rec.get("acos") is None and revenue > 0:
        rec["acos"] = round(spend / revenue, 6)


def _summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"row_count": 0}
    total_spend = sum(r.get("spend") or 0 for r in rows)
    total_rev = sum(r.get("revenue") or 0 for r in rows)
    total_clicks = sum(r.get("clicks") or 0 for r in rows)
    total_impr = sum(r.get("impressions") or 0 for r in rows)
    total_orders = sum(r.get("orders") or 0 for r in rows)
    dates = sorted({r["date"] for r in rows if r.get("date")})
    products = {r["product_id"] for r in rows if r.get("product_id")}
    return {
        "row_count": len(rows),
        "date_range": [dates[0], dates[-1]] if dates else None,
        "products": len(products),
        "total_spend": round(total_spend, 2),
        "total_revenue": round(total_rev, 2),
        "total_clicks": total_clicks,
        "total_impressions": total_impr,
        "total_orders": total_orders,
        "roas": round(total_rev / total_spend, 3) if total_spend else None,
        "ctr": round(total_clicks / total_impr, 5) if total_impr else None,
        "cpc": round(total_spend / total_clicks, 2) if total_clicks else None,
        "acos": round(total_spend / total_rev, 5) if total_rev else None,
    }
