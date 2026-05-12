#!/usr/bin/env python3
"""Send the daily FOG industry briefing email after refresh_data.py runs.

Reads data/last_refresh.json (written by refresh_data.py) for the list of
new news + deal items added in this run. If nothing was added, exits 0
without sending anything — we never want the inbox to fill up with empty
"no updates" notes.

Auth: Gmail SMTP via app password. Required env vars (set as GitHub
Actions secrets):
  - GMAIL_ADDRESS       e.g. "you@gmail.com" (both the From and the SMTP auth user)
  - GMAIL_APP_PASSWORD  16-char app password from accounts.google.com → Security → App passwords
  - RECIPIENT_EMAIL     where the digest goes (can be the same as GMAIL_ADDRESS)

The HTML is intentionally inline-styled — Gmail strips <style> blocks in
many client renderings, so each element carries its own style attribute.
"""

from __future__ import annotations

import html
import json
import os
import smtplib
import ssl
import sys
from datetime import datetime, timezone
from email.message import EmailMessage

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAST_REFRESH_PATH = os.path.join(ROOT, "data", "last_refresh.json")

SITE_URL = "https://naeemm43.github.io/fog-command-center/"
BRAND_NAVY = "#1F3864"

# Category badge colors — kept in sync with build_index.py's news-card CSS
# so the email and the command-center page feel like one product.
CAT_COLORS = {
    "M&A":              "#e74c3c",
    "Regulatory":       "#8E44AD",
    "Renewable Fuels":  "#27ae60",
    "Public Co.":       "#2C3E50",
    "Restaurant":       "#f39c12",
    "Technology":       "#3498db",
    "Labor/Ops":        "#95a5a6",
    "Infrastructure":   "#1abc9c",
    "Industry Events":  "#d35400",
    "ESG":              "#16a085",
}


def esc(s: str | None) -> str:
    return html.escape(str(s or ""), quote=True)


def fmt_date(s: str | None) -> str:
    if not s:
        return "—"
    try:
        return datetime.fromisoformat(s).strftime("%b %d, %Y")
    except ValueError:
        return s


def render_deals_table(deals: list[dict]) -> str:
    if not deals:
        return ""
    deals = sorted(deals, key=lambda x: x.get("date") or "1900-01-01", reverse=True)
    rows = []
    for d in deals:
        rows.append(
            "<tr>"
            f"<td style='padding:8px 10px;border-bottom:1px solid #eee;white-space:nowrap;'>{esc(fmt_date(d.get('date')))}</td>"
            f"<td style='padding:8px 10px;border-bottom:1px solid #eee;'><b>{esc(d.get('target'))}</b></td>"
            f"<td style='padding:8px 10px;border-bottom:1px solid #eee;'>{esc(d.get('acquirer'))}</td>"
            f"<td style='padding:8px 10px;border-bottom:1px solid #eee;color:#555;'>{esc(d.get('location'))}</td>"
            "</tr>"
        )
    return (
        f"<h3 style='color:{BRAND_NAVY};margin:24px 0 8px 0;font-size:16px;border-bottom:2px solid {BRAND_NAVY};padding-bottom:4px;'>"
        f"NEW M&amp;A DEALS ({len(deals)})</h3>"
        "<table cellpadding='0' cellspacing='0' style='width:100%;border-collapse:collapse;font-size:13px;'>"
        "<thead><tr style='background:#f5f7fa;'>"
        "<th style='padding:8px 10px;text-align:left;font-weight:600;color:#444;'>Date</th>"
        "<th style='padding:8px 10px;text-align:left;font-weight:600;color:#444;'>Target</th>"
        "<th style='padding:8px 10px;text-align:left;font-weight:600;color:#444;'>Acquirer</th>"
        "<th style='padding:8px 10px;text-align:left;font-weight:600;color:#444;'>Location</th>"
        "</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def render_news_list(news: list[dict]) -> str:
    if not news:
        return ""
    # Defensive resort by date desc — refresh_data.py already sorts but
    # we don't want the email to be at the mercy of upstream changes.
    news = sorted(news, key=lambda x: x.get("date") or "1900-01-01", reverse=True)
    cards = []
    for n in news:
        cat = n.get("category") or "Industry Events"
        bg = CAT_COLORS.get(cat, "#888")
        source_link = ""
        if n.get("source_url"):
            source_link = (
                f"<a href='{esc(n['source_url'])}' style='color:{BRAND_NAVY};font-size:12px;text-decoration:none;'>"
                f"Source: {esc(n.get('source') or 'link')} →</a>"
            )
        elif n.get("source"):
            source_link = f"<span style='color:#888;font-size:12px;'>Source: {esc(n.get('source'))}</span>"
        relevance = n.get("relevance_score")
        try:
            relevance = int(relevance)
        except (TypeError, ValueError):
            relevance = 3
        rel_badge = (
            "<span style='background:#fef5e7;color:#d35400;font-size:10px;padding:2px 6px;border-radius:3px;margin-left:6px;'>🔥 High Relevance</span>"
            if relevance >= 4 else ""
        )
        tm_alert = (
            f"<div style='background:#fff4d2;border:1px solid #d4a017;padding:6px 10px;border-radius:3px;margin-top:8px;font-size:12px;'>"
            f"⚠️ <b>TIER 2 ALERT:</b> near {esc(n.get('target_market_name') or 'target market')}</div>"
            if n.get("is_target_market") else ""
        )
        cards.append(
            f"<div style='border-left:3px solid {bg};background:#fafafa;padding:12px 14px;margin-bottom:10px;border-radius:0 4px 4px 0;'>"
            f"<div style='font-size:11px;color:#888;margin-bottom:4px;'>"
            f"<span style='background:{bg};color:#fff;padding:2px 6px;border-radius:3px;font-weight:600;'>{esc(cat)}</span>"
            f"&nbsp;&nbsp;{esc(fmt_date(n.get('date')))}{rel_badge}</div>"
            f"<div style='font-size:14px;font-weight:600;line-height:1.35;margin-bottom:6px;color:#222;'>{esc(n.get('headline'))}</div>"
            f"<div style='font-size:13px;color:#444;line-height:1.5;margin-bottom:6px;'>{esc(n.get('summary'))}</div>"
            f"{source_link}{tm_alert}</div>"
        )
    return (
        f"<h3 style='color:{BRAND_NAVY};margin:24px 0 8px 0;font-size:16px;border-bottom:2px solid {BRAND_NAVY};padding-bottom:4px;'>"
        f"NEW TODAY ({len(news)})</h3>" + "".join(cards)
    )


def render_tier2_alerts(news: list[dict], deals: list[dict]) -> str:
    flagged_news = [n for n in news if n.get("is_target_market")]
    flagged_deals = [d for d in deals if d.get("is_target_market")]
    if not flagged_news and not flagged_deals:
        return ""
    lines = []
    for d in flagged_deals:
        lines.append(
            f"<li style='margin-bottom:6px;'><b>Deal:</b> {esc(d.get('target'))} "
            f"acquired by {esc(d.get('acquirer'))} — {esc(d.get('location'))} "
            f"<span style='color:#888;'>({esc(d.get('target_market_name') or 'target market')})</span></li>"
        )
    for n in flagged_news:
        lines.append(
            f"<li style='margin-bottom:6px;'><b>News:</b> {esc(n.get('headline'))} "
            f"<span style='color:#888;'>({esc(n.get('target_market_name') or 'target market')})</span></li>"
        )
    return (
        f"<div style='background:#fff4d2;border:1px solid #d4a017;border-radius:4px;padding:12px 16px;margin:16px 0;'>"
        f"<h3 style='margin:0 0 8px 0;color:#8a6a00;font-size:14px;'>⚠️ TIER 2 MARKET ALERTS ({len(lines)})</h3>"
        f"<ul style='margin:0;padding-left:20px;font-size:13px;color:#444;'>{''.join(lines)}</ul></div>"
    )


def build_empty_html() -> str:
    """Short body sent on days when refresh_data.py found zero new items.
    Better than silence — confirms the pipeline ran and there just wasn't
    anything to report."""
    today = datetime.now(timezone.utc).strftime("%B %d, %Y")
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#f0f2f5;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#222;">
<div style="max-width:720px;margin:0 auto;background:#fff;">
  <div style="background:{BRAND_NAVY};color:#fff;padding:24px 28px;">
    <div style="font-size:13px;letter-spacing:1px;color:#8FAADC;text-transform:uppercase;margin-bottom:4px;">FOG Industry Command Center</div>
    <div style="font-size:22px;font-weight:600;">Daily Briefing — {esc(today)}</div>
  </div>
  <div style="padding:28px;text-align:center;">
    <p style="font-size:15px;color:#444;margin:0 0 16px 0;">No new FOG industry news or deals found in the last 24 hours.</p>
    <p style="font-size:13px;color:#888;margin:0;">The refresh pipeline ran successfully; no headlines met the recency cutoff.</p>
  </div>
  <div style="background:#f5f7fa;padding:18px 28px;border-top:1px solid #e0e4ea;font-size:12px;color:#666;text-align:center;">
    <a href="{SITE_URL}" style="color:{BRAND_NAVY};text-decoration:none;font-weight:600;">View Command Center →</a>
    <div style="margin-top:8px;color:#888;font-size:11px;">This briefing is auto-generated. Reply with questions.</div>
  </div>
</div>
</body></html>
"""


def build_html(summary: dict) -> str:
    new_news = summary.get("new_news") or []
    new_deals = summary.get("new_deals") or []
    today = datetime.now(timezone.utc).strftime("%B %d, %Y")
    deals_section = render_deals_table(new_deals)
    tier2_section = render_tier2_alerts(new_news, new_deals)
    news_section = render_news_list(new_news)
    summary_line = (
        f"Added <b>{len(new_news)}</b> news item{'s' if len(new_news) != 1 else ''} "
        f"and <b>{len(new_deals)}</b> deal{'s' if len(new_deals) != 1 else ''} today."
    )

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#f0f2f5;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#222;">
<div style="max-width:720px;margin:0 auto;background:#fff;">
  <div style="background:{BRAND_NAVY};color:#fff;padding:24px 28px;">
    <div style="font-size:13px;letter-spacing:1px;color:#8FAADC;text-transform:uppercase;margin-bottom:4px;">FOG Industry Command Center</div>
    <div style="font-size:22px;font-weight:600;">Daily Briefing — {esc(today)}</div>
  </div>
  <div style="padding:20px 28px;">
    <p style="font-size:14px;color:#444;margin:0 0 8px 0;">{summary_line}</p>
    {tier2_section}
    {deals_section}
    {news_section}
  </div>
  <div style="background:#f5f7fa;padding:18px 28px;border-top:1px solid #e0e4ea;font-size:12px;color:#666;text-align:center;">
    <a href="{SITE_URL}" style="color:{BRAND_NAVY};text-decoration:none;font-weight:600;">View Command Center →</a>
    <div style="margin-top:8px;color:#888;font-size:11px;">This briefing is auto-generated. Reply with questions.</div>
  </div>
</div>
</body></html>
"""


def main() -> int:
    if not os.path.exists(LAST_REFRESH_PATH):
        sys.stderr.write(f"no {LAST_REFRESH_PATH} — refresh_data.py hasn't run yet; nothing to send\n")
        return 0
    with open(LAST_REFRESH_PATH, encoding="utf-8") as f:
        summary = json.load(f)

    added = (summary.get("added_news_count", 0) or 0) + (summary.get("added_deals_count", 0) or 0)

    gmail_addr = os.environ.get("GMAIL_ADDRESS")
    gmail_pw = os.environ.get("GMAIL_APP_PASSWORD")
    recipient = os.environ.get("RECIPIENT_EMAIL")
    missing = [k for k, v in [
        ("GMAIL_ADDRESS", gmail_addr),
        ("GMAIL_APP_PASSWORD", gmail_pw),
        ("RECIPIENT_EMAIL", recipient),
    ] if not v]
    if missing:
        sys.stderr.write(f"missing env: {', '.join(missing)} — cannot send email\n")
        return 1

    today = datetime.now(timezone.utc).strftime("%B %d, %Y")
    msg = EmailMessage()
    msg["Subject"] = f"FOG Industry Daily Briefing — {today}"
    msg["From"] = gmail_addr
    msg["To"] = recipient

    if added == 0:
        msg.set_content(
            "No new FOG industry news or deals found in the last 24 hours. "
            f"Visit the command center: {SITE_URL}"
        )
        msg.add_alternative(build_empty_html(), subtype="html")
        print(f"Sending empty-day digest → {recipient}")
    else:
        msg.set_content(
            "This email's HTML version contains the daily FOG industry briefing. "
            f"Open in an HTML-capable client to view, or visit {SITE_URL}"
        )
        msg.add_alternative(build_html(summary), subtype="html")
        print(f"Sending digest: {summary['added_news_count']} news, "
              f"{summary['added_deals_count']} deals → {recipient}")
    ctx = ssl.create_default_context()
    with smtplib.SMTP("smtp.gmail.com", 587) as s:
        s.starttls(context=ctx)
        s.login(gmail_addr, gmail_pw)
        s.send_message(msg)
    print("Sent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
