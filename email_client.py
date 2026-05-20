"""
Email 寄送封裝（Gmail SMTP）
─────────────────────────
責任：把月報資料 render 成 HTML email、用 Gmail SMTP 寄出。
     沒設環境變數時自動 fallback 為 mock，不影響開發/Demo。

對外公開函式：
    is_configured() → bool
    config_status() → dict
    render_monthly_report_html(report_data) → str
    send_monthly_report(to_email, subject, report_data, attachment_pdf_bytes=None)
        → {"status": "sent"|"mock"|"error", "message_id": str|None, "error": str|None}

環境變數：
    GMAIL_USER        寄件 Gmail 帳號（例：yourname@gmail.com）
    GMAIL_APP_PASSWORD  Gmail App 密碼（16 碼，不是登入密碼）
"""
from __future__ import annotations

import os
import smtplib
import uuid
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587


def is_configured() -> bool:
    return bool(os.environ.get("GMAIL_USER")) and bool(os.environ.get("GMAIL_APP_PASSWORD"))


def config_status() -> dict:
    return {
        "provider": "gmail_smtp",
        "gmail_user": os.environ.get("GMAIL_USER") or None,
        "app_password_set": bool(os.environ.get("GMAIL_APP_PASSWORD")),
        "ready": is_configured(),
        "mode": "real" if is_configured() else "mock",
    }


# ─────────────────────────── HTML email render ───────────────────────────

def _fmt_money(n) -> str:
    if n is None:
        return "—"
    return f"NT${int(n):,}"


def render_monthly_report_html(report: dict) -> str:
    """把 /reports/monthly/{cid} 回的 dict 轉成 email HTML。
    保持 inline styles（多數 email client 不支援 <style> tag）。
    """
    cust = report.get("customer", {})
    m = report.get("metrics", {})
    profit = report.get("profit") or {}
    top = report.get("top_products") or []
    highlights = report.get("highlights") or []
    plans = report.get("next_month_plan") or []
    month = report.get("month", "")

    src = (report.get("_meta") or {}).get("source", "mock")
    src_badge = (
        '<span style="display:inline-block;padding:3px 10px;background:#16A34A;color:#fff;border-radius:12px;font-size:11px">● 真實 CSV 資料</span>'
        if src == "real" else
        '<span style="display:inline-block;padding:3px 10px;background:#94A3B8;color:#fff;border-radius:12px;font-size:11px">○ Demo 範例資料</span>'
    )

    achieved_html = (
        '<span style="color:#16A34A;font-weight:600">✓ 達標</span>'
        if m.get("kpi_achieved") else
        '<span style="color:#DC2626;font-weight:600">✗ 未達標</span>'
    )

    profit_block = ""
    if profit:
        net = profit.get("net_profit", 0)
        net_color = "#16A34A" if net >= 0 else "#DC2626"
        profit_block = f"""
        <div style="margin:20px 0;padding:16px;background:#FEF3C7;border-radius:8px;border-left:4px solid #D97706">
          <div style="font-weight:600;margin-bottom:10px;color:#92400E">💰 真實獲利分解</div>
          <table style="width:100%;font-size:13px;color:#78350F">
            <tr><td>營收</td><td style="text-align:right"><strong>{_fmt_money(profit.get('revenue'))}</strong></td></tr>
            <tr><td>− 廣告花費</td><td style="text-align:right">−{_fmt_money(profit.get('ad_spend'))}</td></tr>
            <tr><td>− 商品成本</td><td style="text-align:right">−{_fmt_money(profit.get('cogs'))}</td></tr>
            <tr><td>− 運費</td><td style="text-align:right">−{_fmt_money(profit.get('shipping_total'))}</td></tr>
            <tr><td>− 平台抽成</td><td style="text-align:right">−{_fmt_money(profit.get('platform_fee'))}</td></tr>
            <tr><td>− 刷卡費</td><td style="text-align:right">−{_fmt_money(profit.get('payment_fee'))}</td></tr>
            <tr style="border-top:1px solid #FCD34D"><td style="padding-top:6px"><strong>淨利</strong></td>
              <td style="text-align:right;padding-top:6px"><strong style="color:{net_color};font-size:15px">{_fmt_money(net)}</strong></td></tr>
          </table>
        </div>"""

    top_block = ""
    if top:
        rows = "".join(
            f'<tr><td style="padding:6px 8px;border-bottom:1px solid #E2E8F0">{i+1}. {p["product_name"]}</td>'
            f'<td style="padding:6px 8px;border-bottom:1px solid #E2E8F0;text-align:right">{_fmt_money(p["revenue"])}</td>'
            f'<td style="padding:6px 8px;border-bottom:1px solid #E2E8F0;text-align:right">{_fmt_money(p["spend"])}</td>'
            f'<td style="padding:6px 8px;border-bottom:1px solid #E2E8F0;text-align:right">{p["roas"]}</td></tr>'
            for i, p in enumerate(top[:5])
        )
        top_block = f"""
        <div style="margin:20px 0">
          <div style="font-weight:600;margin-bottom:8px">🏆 本月 Top 商品</div>
          <table style="width:100%;font-size:13px;border-collapse:collapse">
            <thead><tr style="background:#F8FAFC">
              <th style="padding:6px 8px;text-align:left">商品</th>
              <th style="padding:6px 8px;text-align:right">營收</th>
              <th style="padding:6px 8px;text-align:right">花費</th>
              <th style="padding:6px 8px;text-align:right">ROAS</th>
            </tr></thead>
            <tbody>{rows}</tbody>
          </table>
        </div>"""

    highlights_html = "".join(f'<li style="margin:4px 0">{h}</li>' for h in highlights)
    plans_html = "".join(f'<li style="margin:4px 0">{p}</li>' for p in plans)

    return f"""<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:#F8FAFC;font-family:-apple-system,'Segoe UI',Microsoft JhengHei,sans-serif;color:#1E293B">
<div style="max-width:680px;margin:20px auto;background:#fff;padding:24px;border-radius:8px;border:1px solid #E2E8F0">
  <div style="margin-bottom:12px">{src_badge}</div>
  <h2 style="color:#EE4D2D;margin:0 0 4px;font-size:22px">📊 {cust.get('name','-')} · {month} 月報</h2>
  <div style="color:#64748B;font-size:13px;margin-bottom:20px">
    合約期 {cust.get('contract_start','-')} ~ {cust.get('contract_end','-')} ·
    服務店家 {' / '.join(cust.get('shop_ids',[])) or '—'}
  </div>

  <table style="width:100%;margin-bottom:16px">
    <tr>
      <td style="width:33%;padding:12px;background:#F8FAFC;border-radius:6px;text-align:center">
        <div style="font-size:11px;color:#64748B">營收</div>
        <div style="font-size:20px;font-weight:600">{_fmt_money(m.get('revenue'))}</div>
      </td>
      <td style="width:2%"></td>
      <td style="width:33%;padding:12px;background:#F8FAFC;border-radius:6px;text-align:center">
        <div style="font-size:11px;color:#64748B">廣告花費</div>
        <div style="font-size:20px;font-weight:600">{_fmt_money(m.get('spend'))}</div>
      </td>
      <td style="width:2%"></td>
      <td style="width:33%;padding:12px;background:#F8FAFC;border-radius:6px;text-align:center">
        <div style="font-size:11px;color:#64748B">ROAS</div>
        <div style="font-size:20px;font-weight:600">{m.get('roas','-')}</div>
        <div style="font-size:11px;color:#64748B;margin-top:2px">目標 {m.get('target_roas','-')} · {achieved_html}</div>
      </td>
    </tr>
  </table>

  {profit_block}
  {top_block}

  <div style="margin:20px 0">
    <div style="font-weight:600;margin-bottom:6px">📌 本月亮點</div>
    <ul style="margin:0;padding-left:20px;font-size:13px">{highlights_html}</ul>
  </div>

  <div style="margin:20px 0">
    <div style="font-weight:600;margin-bottom:6px">🎯 下月計畫</div>
    <ul style="margin:0;padding-left:20px;font-size:13px">{plans_html}</ul>
  </div>

  <div style="margin-top:30px;padding-top:14px;border-top:1px solid #E2E8F0;font-size:11px;color:#94A3B8;text-align:center">
    本月報由蝦皮廣告代操儀表板自動產生 · 如有疑問請與您的代操顧問聯絡
  </div>
</div>
</body></html>"""


# ─────────────────────────── 寄送 ───────────────────────────

def send_monthly_report(
    to_email: str,
    subject: str,
    report_data: dict,
    attachment_pdf_bytes: bytes | None = None,
) -> dict:
    """寄送月報。沒設定 Gmail 憑證時 fallback 為 mock。"""
    if not is_configured():
        return {
            "status": "mock",
            "message_id": None,
            "error": None,
            "note": "Gmail 未設定，未實際寄送（mock 模式）。請設定環境變數 GMAIL_USER 與 GMAIL_APP_PASSWORD",
        }

    html_body = render_monthly_report_html(report_data)
    gmail_user = os.environ["GMAIL_USER"]
    app_password = os.environ["GMAIL_APP_PASSWORD"]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = gmail_user
    msg["To"] = to_email
    msg["Message-ID"] = f"<{uuid.uuid4()}@shopee-dashboard>"
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    if attachment_pdf_bytes:
        part = MIMEApplication(attachment_pdf_bytes, Name=f"{subject}.pdf")
        part["Content-Disposition"] = f'attachment; filename="{subject}.pdf"'
        msg.attach(part)

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.login(gmail_user, app_password)
            server.sendmail(gmail_user, to_email, msg.as_string())
        return {
            "status": "sent",
            "message_id": msg["Message-ID"],
            "error": None,
        }
    except Exception as e:
        return {"status": "error", "message_id": None, "error": f"{type(e).__name__}: {e}"}
