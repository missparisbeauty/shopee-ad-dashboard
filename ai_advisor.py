"""
AI 廣告代操顧問
─────────────────────────
責任：吃儀表板真實數據（KPI + 真實獲利 + Top 商品 + 規則觸發），
     用 Claude API 產生「診斷 + 立即動作 + 本週計畫」三段結構化建議。

對外公開函式：
    is_configured() → bool
    config_status() → dict
    generate_insights(report_data, customer=None) → dict

未設 ANTHROPIC_API_KEY 時自動 fallback「規則式分析」 — 用簡單 if/else
依據相同數據規則產生建議，比 mock 假資料有用，比 LLM 便宜。
"""
from __future__ import annotations

import json
import os
from typing import Any

try:
    import anthropic
    _HAS_SDK = True
except ImportError:
    _HAS_SDK = False

# 可在環境變數覆寫
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
MAX_TOKENS = 1024


def is_configured() -> bool:
    return _HAS_SDK and bool(os.environ.get("ANTHROPIC_API_KEY"))


def config_status() -> dict:
    return {
        "sdk_installed": _HAS_SDK,
        "api_key_set": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "model": MODEL,
        "ready": is_configured(),
        "mode": "real" if is_configured() else "rule_based",
    }


# ─────────────────────────── System prompt（cache 用） ───────────────────────────

SYSTEM_PROMPT = """你是蝦皮廣告代操業務的資深顧問，專長：
- 解讀廣告數據（ROAS、CTR、ACoS、CPC）找出問題根因
- 用真實獲利（扣完商品成本/運費/平台抽成）判斷是否真的賺錢
- 給出可執行的下一步（不是空話，例如「P10001 ROAS 1.2 → 把出價從 X 降到 Y」）

回應格式必須是 JSON，包含三個 array：
{
  "diagnosis": ["...", "..."],          // 1-3 條診斷（最重要的問題）
  "immediate_actions": ["...", "..."],   // 1-3 條今天就該做的事（具體動作）
  "this_week_plan": ["...", "..."]       // 1-3 條本週要做的優化
}

語言：繁體中文、口語化、避免術語。每條建議含具體數字。"""


# ─────────────────────────── 規則式 fallback ───────────────────────────

def _rule_based_insights(report_data: dict) -> dict:
    """沒 API key 時的規則式分析。雖然簡單但有用。"""
    kpi = report_data.get("kpi") or report_data.get("metrics") or {}
    profit = report_data.get("profit") or {}
    top = report_data.get("top_products") or []
    triggered = report_data.get("triggered_rules") or []

    roas = kpi.get("roas", 0) or 0
    target_roas = kpi.get("target_roas", 3.0)
    net_profit = profit.get("net_profit", 0) or 0
    spend = kpi.get("spend", 0) or 0

    diagnosis = []
    immediate = []
    weekly = []

    # 診斷
    if net_profit < 0:
        diagnosis.append(f"❌ 整體淨利為負（{net_profit:,}），廣告營收扣完成本反而虧損 {-net_profit:,}")
    elif net_profit > 0 and roas < 1:
        diagnosis.append(f"⚠️ ROAS {roas} < 1（廣告每 1 元只回收 {roas} 元），雖然淨利 {net_profit:,} 但要小心")
    if roas < target_roas:
        diff = round((target_roas - roas) / target_roas * 100, 0)
        diagnosis.append(f"❌ ROAS {roas} 低於目標 {target_roas}（差 {diff}%）")
    if spend > 0 and len(top) >= 2:
        top1, top2 = top[0], top[1]
        if top1.get("revenue", 0) > top2.get("revenue", 0) * 3:
            diagnosis.append(f"⚠️ 營收過度集中：{top1['product_name']} 營收是第二名的 {round(top1['revenue']/top2['revenue'],1)} 倍")

    # 立即動作
    losing = [p for p in top if (p.get("roas", 0) or 0) < 1]
    if losing:
        for p in losing[:2]:
            immediate.append(f"立刻檢查 {p['product_name']}（ROAS {p['roas']}）— 出價降 30% 或暫停廣告")
    if triggered:
        immediate.append(f"已有 {len(triggered)} 條自動規則觸發 — 到「預算與自動規則」頁面確認執行")

    # 本週計畫
    winning = [p for p in top if (p.get("roas", 0) or 0) >= target_roas * 1.2]
    if winning:
        weekly.append(f"加碼預算到表現最好的商品：{', '.join(p['product_name'] for p in winning[:2])}（ROAS 都超標 20%）")
    if profit.get("shipping_total", 0) > profit.get("revenue", 1) * 0.15:
        weekly.append("運費佔營收 15% 以上 — 考慮設「滿額免運」門檻或改寄超商取貨降運費")
    if not immediate and not weekly:
        weekly.append("整體表現穩定，本週維持當前策略，可測試 3 組新關鍵字擴量")

    if not diagnosis:
        diagnosis.append("本期 KPI 表現符合或超越目標 ✓")
    if not immediate:
        immediate.append("無緊急事項")

    return {
        "diagnosis": diagnosis[:3],
        "immediate_actions": immediate[:3],
        "this_week_plan": weekly[:3],
        "_meta": {"source": "rule_based", "model": None},
    }


# ─────────────────────────── Claude API ───────────────────────────

def _claude_insights(report_data: dict, customer: dict | None = None) -> dict:
    client = anthropic.Anthropic()
    cust_name = (customer or {}).get("name", "未知客戶")
    target_roas = (customer or {}).get("target_roas", 3.0)

    # 縮減 input 只送必要資訊
    kpi = report_data.get("kpi") or report_data.get("metrics") or {}
    profit = report_data.get("profit") or {}
    top = report_data.get("top_products") or []
    triggered = report_data.get("triggered_rules") or []

    user_payload = {
        "客戶": cust_name,
        "目標_ROAS": target_roas,
        "KPI": {
            "spend": kpi.get("spend"), "revenue": kpi.get("revenue"),
            "roas": kpi.get("roas"), "ctr": kpi.get("ctr"),
            "orders": kpi.get("orders"),
        },
        "真實獲利": {
            "revenue": profit.get("revenue"), "ad_spend": profit.get("ad_spend"),
            "cogs": profit.get("cogs"), "shipping": profit.get("shipping_total"),
            "platform_fee": profit.get("platform_fee"),
            "net_profit": profit.get("net_profit"),
        } if profit else None,
        "Top商品": [
            {"name": p.get("product_name"), "spend": p.get("spend"),
             "revenue": p.get("revenue"), "roas": p.get("roas"),
             "orders": p.get("orders")}
            for p in top[:5]
        ],
        "已觸發規則": [
            {"rule": t.get("rule") or t.get("rule_name"),
             "action": t.get("action"), "影響商品數": t.get("affected_count", 0)}
            for t in triggered[:5]
        ],
    }

    msg = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=[
            {"type": "text", "text": SYSTEM_PROMPT,
             "cache_control": {"type": "ephemeral"}},
        ],
        messages=[{
            "role": "user",
            "content": "請根據以下儀表板數據，產生 JSON 格式的廣告優化建議：\n\n"
                       + json.dumps(user_payload, ensure_ascii=False, indent=2),
        }],
    )

    raw = "".join(b.text for b in msg.content if hasattr(b, "text"))

    # 嘗試 parse JSON（Claude 通常會包在 ```json ... ``` 或直接純 JSON）
    parsed = _extract_json(raw)
    if not parsed:
        return {
            "diagnosis": ["（AI 回應解析失敗，回傳原文）"],
            "immediate_actions": [raw[:500]],
            "this_week_plan": [],
            "_meta": {"source": "claude", "model": MODEL,
                      "parse_error": True,
                      "usage": _usage(msg)},
        }

    parsed["_meta"] = {
        "source": "claude", "model": MODEL,
        "usage": _usage(msg),
    }
    return parsed


def _usage(msg) -> dict:
    u = getattr(msg, "usage", None)
    if not u:
        return {}
    return {
        "input_tokens": getattr(u, "input_tokens", 0),
        "output_tokens": getattr(u, "output_tokens", 0),
        "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
        "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
    }


def _extract_json(text: str) -> dict | None:
    """從可能含 markdown code block 的文字中萃取 JSON"""
    text = text.strip()
    # 去 markdown code fence
    if text.startswith("```"):
        first_nl = text.find("\n")
        if first_nl > 0:
            text = text[first_nl + 1:]
        if text.endswith("```"):
            text = text[:-3]
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # 找第一個 { 到最後 } 看看
        s, e = text.find("{"), text.rfind("}")
        if s != -1 and e > s:
            try:
                return json.loads(text[s:e + 1])
            except json.JSONDecodeError:
                return None
    return None


# ─────────────────────────── 主入口 ───────────────────────────

def generate_insights(report_data: dict, customer: dict | None = None) -> dict:
    """主入口。沒設定 API key 自動 fallback 規則式分析。"""
    if not is_configured():
        return _rule_based_insights(report_data)
    try:
        return _claude_insights(report_data, customer)
    except Exception as e:
        # API 失敗時回 rule_based 並標註錯誤
        result = _rule_based_insights(report_data)
        result["_meta"]["claude_error"] = f"{type(e).__name__}: {e}"
        result["_meta"]["fallback"] = True
        return result
