#!/usr/bin/env python3
"""Send the daily FOG industry briefing email after refresh_data.py runs.

Reads data/last_refresh.json (written by refresh_data.py) for the list of
new news + deal items added in this run. Always sends an email — quiet
days produce a short "no activity" note rather than silence — so
external recipients always know the pipeline ran today.

Auth: Gmail SMTP via app password. Required env vars (set as GitHub
Actions secrets):
  - GMAIL_ADDRESS       e.g. "you@gmail.com" — used for both the From
                        header and SMTP auth user.
  - GMAIL_APP_PASSWORD  16-char app password from accounts.google.com
                        → Security → App passwords.
  - RECIPIENT_EMAIL     comma-separated. FIRST address goes in the To:
                        header; the rest land in Bcc so external
                        recipients don't see each other's addresses.
                        The first address should always be the owner.

The HTML is intentionally inline-styled — Gmail strips <style> blocks
in many client renderings, so each element carries its own style
attribute.
"""

from __future__ import annotations

import html
import json
import os
import re
import smtplib
import ssl
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAST_REFRESH_PATH = os.path.join(ROOT, "data", "last_refresh.json")
NEWS_FEED_PATH = os.path.join(ROOT, "data", "news_feed.json")
NEWS_ARCHIVE_PATH = os.path.join(ROOT, "data", "news_archive.json")

SITE_URL = "https://naeemm43.github.io/fog-command-center/"
BRAND_NAVY = "#1F3864"
FROM_DISPLAY_NAME = "FOG Industry Briefing"
CURATOR_NAME = "Naeem Muscatwalla"
CURATOR_EMAIL = "naeemm43@gmail.com"

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


def fmt_date_relative(s: str | None, today: "datetime | None" = None) -> str:
    """Like fmt_date but renders 'TODAY' / 'YESTERDAY' for the two most
    recent buckets — makes article freshness immediately visible on
    each card. Anything two-plus days old falls back to the standard
    'Mon DD, YYYY' format."""
    if not s:
        return "—"
    try:
        d = datetime.fromisoformat(s).date()
    except (ValueError, TypeError):
        return s
    ref = (today or datetime.now(timezone.utc)).date() if hasattr(today, "date") else (today or datetime.now(timezone.utc).date())
    delta = (ref - d).days
    if delta == 0:
        return "TODAY"
    if delta == 1:
        return "YESTERDAY"
    return d.strftime("%b %d, %Y")


# ============================================================================
# Pre-flight validation. The briefing goes to external counterparties now;
# broken links and ragged content look unprofessional. We drop individual
# bad items quietly and only fall back to the maintenance template if the
# email would be almost entirely empty after filtering.
# ============================================================================

# Strip tags from any user-visible string. Anthropic web_search occasionally
# leaves <cite index="...">...</cite> or HTML in headlines/summaries even
# after the upstream cleaner runs. We use this regex both as a detector
# (validation) and as a one-shot stripper (auto-fix).
_TAG_RX = re.compile(r"</?[A-Za-z][^>]*>|antml:cite\b", re.IGNORECASE)
_BAD_CHAR_RX = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F]")

# Network failure modes that mean a link is actually broken. Anti-bot
# responses (403/405/406/429) are NOT counted as broken — many legitimate
# news sites block HEAD requests but render fine in a browser.
_DEAD_HTTP_CODES = {400, 404, 410}


def _check_url(url: str, timeout: float = 5.0) -> bool:
    """Return False only when the URL is unambiguously broken (connection
    failure, DNS failure, or a 4xx that means "not found"). Anti-bot and
    auth-wall responses pass — we don't want to drop legitimate articles
    from a paywalled publication."""
    if not url:
        return True
    req = urllib.request.Request(
        url, method="HEAD",
        headers={"User-Agent": "Mozilla/5.0 (compatible; FOG-Briefing/1.0)"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", 200)
            return status not in _DEAD_HTTP_CODES
    except urllib.error.HTTPError as e:
        return e.code not in _DEAD_HTTP_CODES
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
        return False
    except Exception:
        # Defensive: any other parser/protocol error → don't trust the link.
        return False


def _has_bad_text(*fields: str | None) -> str | None:
    """Return a reason string if any field has HTML/citation tags or
    control characters; None when everything looks clean."""
    for f in fields:
        if not f:
            continue
        if _TAG_RX.search(f):
            return "tag/citation marker present"
        if _BAD_CHAR_RX.search(f):
            return "control character present"
    return None


def validate_news_items(items: list[dict], dead_urls: set[str]) -> list[dict]:
    """Return only the items safe to publish. Reasons for dropping are
    written to stderr so a maintainer auditing the run can see exactly
    what got filtered."""
    out: list[dict] = []
    for n in items:
        bad = _has_bad_text(n.get("headline"), n.get("summary"))
        if bad:
            sys.stderr.write(f"  drop news: {bad} | {(n.get('headline') or '')[:80]}\n")
            continue
        url = n.get("source_url") or ""
        if url and url in dead_urls:
            sys.stderr.write(f"  drop news: broken URL ({url}) | {(n.get('headline') or '')[:80]}\n")
            continue
        out.append(n)
    return out


def validate_deals(deals: list[dict]) -> list[dict]:
    """Deal table rows that don't have a date, target, and acquirer look
    visually broken in the email — drop them."""
    out: list[dict] = []
    for d in deals:
        if not (d.get("date") or "").strip():
            sys.stderr.write(f"  drop deal: empty date | target={d.get('target')!r}\n")
            continue
        if not (d.get("target") or "").strip():
            sys.stderr.write(f"  drop deal: empty target | date={d.get('date')!r}\n")
            continue
        if not (d.get("acquirer") or "").strip():
            sys.stderr.write(f"  drop deal: empty acquirer | target={d.get('target')!r}\n")
            continue
        bad = _has_bad_text(d.get("target"), d.get("acquirer"), d.get("location"))
        if bad:
            sys.stderr.write(f"  drop deal: {bad} | target={d.get('target')!r}\n")
            continue
        out.append(d)
    return out


def preflight_deal_news_sync(summary: dict) -> int:
    """Verify every deal added in this run has a paired news article.
    If any are missing (which shouldn't happen with the refresh_data.py
    sync invariant in place), synthesize them, persist to
    news_feed.json, and inject into the summary so the email reflects
    the fix. Returns the count of synthesized entries."""
    new_deals = summary.get("new_deals") or []
    if not new_deals:
        return 0

    # Import lazily — keeps send_email_digest.py runnable as a leaf module
    # even if refresh_data.py is broken.
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        import refresh_data as rd
    except Exception as e:
        sys.stderr.write(f"  preflight: cannot import refresh_data ({e}); skipping deal-news sync\n")
        return 0

    try:
        with open(NEWS_FEED_PATH, encoding="utf-8") as f:
            feed = json.load(f)
        with open(NEWS_ARCHIVE_PATH, encoding="utf-8") as f:
            archive = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        sys.stderr.write(f"  preflight: cannot load news files ({e}); skipping deal-news sync\n")
        return 0

    today_iso = datetime.now(timezone.utc).date().isoformat()
    all_news = feed + archive
    synthesized: list[dict] = []
    for d in new_deals:
        if rd._deal_has_news(d, all_news):
            continue
        sys.stderr.write(
            f"  preflight: deal missing news, synthesizing — "
            f"{d.get('acquirer','?')} ↔ {d.get('target','?')[:60]}\n"
        )
        n = rd.synthesize_news_from_deal(d, today_iso)
        feed.append(n)
        all_news.append(n)
        synthesized.append(n)

    if synthesized:
        feed.sort(key=lambda x: x.get("date") or "1900-01-01", reverse=True)
        with open(NEWS_FEED_PATH, "w", encoding="utf-8") as f:
            json.dump(feed, f, indent=2)
        # Route each synthesized item to the right summary bucket based
        # on the underlying deal's date. A same-day deal lands in NEW
        # TODAY; a deal from 3 days ago lands in RECENT UPDATES; a deal
        # older than 7 days lands in backfill (won't appear in the
        # email, but the news entry still exists in the feed file).
        today = datetime.now(timezone.utc).date()
        new_today_bucket = summary.setdefault("new_today_news", [])
        recent_bucket = summary.setdefault("recent_news", [])
        backfill_bucket = summary.setdefault("backfill_news", [])
        for n in synthesized:
            try:
                age = (today - datetime.fromisoformat(n["date"]).date()).days
            except (KeyError, ValueError, TypeError):
                age = 0
            if age <= 1:
                new_today_bucket.append(n)
            elif age <= 7:
                recent_bucket.append(n)
            else:
                backfill_bucket.append(n)
        summary["added_news_count"] = (summary.get("added_news_count", 0) or 0) + len(synthesized)

    return len(synthesized)


def precheck_urls(items: list[dict]) -> set[str]:
    """HEAD-check every distinct URL in the candidate list. Returns the
    set of URLs that are unambiguously dead."""
    urls = {n.get("source_url") for n in items if n.get("source_url")}
    dead: set[str] = set()
    for u in urls:
        if not _check_url(u):
            dead.add(u)
    if dead:
        sys.stderr.write(f"  pre-flight: {len(dead)} broken URL(s) detected\n")
    return dead


# ============================================================================
# HTML builders
# ============================================================================

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


def render_news_list(news: list[dict], heading: str = "NEW TODAY",
                      heading_color: str | None = None) -> str:
    if not news:
        return ""
    news = sorted(news, key=lambda x: x.get("date") or "1900-01-01", reverse=True)
    cards = []
    for n in news:
        # Multi-category support: prefer the new categories array, fall
        # back to the legacy single category for items written before
        # the multi-tag rollout.
        cats = n.get("categories") or ([n.get("category")] if n.get("category") else [])
        cats = [c for c in cats if c]
        if not cats:
            cats = ["Industry Events"]
        cat = cats[0]
        bg = CAT_COLORS.get(cat, "#888")
        badges_html = "".join(
            f"<span style='background:{CAT_COLORS.get(c, '#888')};color:#fff;"
            f"padding:2px 6px;border-radius:3px;font-weight:600;"
            f"margin-right:4px;'>{esc(c)}</span>"
            for c in cats
        )
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
        # Relative date label — "TODAY" / "YESTERDAY" / "May 13, 2026"
        # — makes the article's freshness obvious without the reader
        # having to read each ISO date.
        date_label = fmt_date_relative(n.get("date"))
        cards.append(
            f"<div style='border-left:3px solid {bg};background:#fafafa;padding:12px 14px;margin-bottom:10px;border-radius:0 4px 4px 0;'>"
            f"<div style='font-size:11px;color:#888;margin-bottom:4px;'>"
            f"{badges_html}"
            f"&nbsp;&nbsp;<span style='font-weight:600;color:#555;'>{esc(date_label)}</span>{rel_badge}</div>"
            f"<div style='font-size:14px;font-weight:600;line-height:1.35;margin-bottom:6px;color:#222;'>{esc(n.get('headline'))}</div>"
            f"<div style='font-size:13px;color:#444;line-height:1.5;margin-bottom:6px;'>{esc(n.get('summary'))}</div>"
            f"{source_link}{tm_alert}</div>"
        )
    color = heading_color or BRAND_NAVY
    return (
        f"<h3 style='color:{color};margin:24px 0 8px 0;font-size:16px;border-bottom:2px solid {color};padding-bottom:4px;'>"
        f"{esc(heading)} ({len(news)})</h3>" + "".join(cards)
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


def render_footer(refreshed_ts: str | None = None) -> str:
    """Shared footer used by all three email templates (digest, quiet,
    maintenance). Includes the View link, the unsubscribe instruction,
    and a small signature block with the last-refreshed timestamp."""
    if refreshed_ts:
        try:
            ts_pretty = (datetime.fromisoformat(refreshed_ts.replace("Z", "+00:00"))
                         .strftime("%Y-%m-%d %H:%M UTC"))
        except (ValueError, AttributeError):
            ts_pretty = refreshed_ts
    else:
        ts_pretty = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"""
  <div style="background:#f5f7fa;padding:18px 28px;border-top:1px solid #e0e4ea;font-size:12px;color:#666;text-align:center;">
    <div><a href="{SITE_URL}" style="color:{BRAND_NAVY};text-decoration:none;font-weight:600;">View Command Center →</a></div>
    <div style="margin-top:18px;padding-top:14px;border-top:1px solid #e0e4ea;color:#444;font-size:12px;line-height:1.7;">
      <div style="font-weight:600;color:#1F3864;">FOG Industry Command Center</div>
      <div style="color:#555;">Curated by {esc(CURATOR_NAME)}</div>
      <div style="color:#555;">Contact: <a href="mailto:{esc(CURATOR_EMAIL)}" style="color:{BRAND_NAVY};text-decoration:none;">{esc(CURATOR_EMAIL)}</a></div>
    </div>
    <div style="margin-top:14px;color:#888;font-size:11px;">
      To unsubscribe, reply with UNSUBSCRIBE in the subject line.
    </div>
    <div style="margin-top:14px;color:#999;font-size:10px;">
      Automated weekday briefing covering the U.S. non-hazardous liquid waste industry — last refreshed {esc(ts_pretty)}.
    </div>
  </div>"""


def _shell(title: str, body_inner: str, refreshed_ts: str | None) -> str:
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#f0f2f5;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#222;">
<div style="max-width:720px;margin:0 auto;background:#fff;">
  <div style="background:{BRAND_NAVY};color:#fff;padding:24px 28px;">
    <div style="font-size:13px;letter-spacing:1px;color:#8FAADC;text-transform:uppercase;margin-bottom:4px;">FOG Industry Command Center</div>
    <div style="font-size:22px;font-weight:600;">{esc(title)}</div>
  </div>
  {body_inner}
  {render_footer(refreshed_ts)}
</div>
</body></html>
"""


def build_quiet_html(refreshed_ts: str | None) -> str:
    """Quiet-day body. Sent when 0 new today-news AND 0 new deals — the
    pipeline ran but had nothing material to report. Better than silence:
    external recipients always know the system is alive."""
    today = datetime.now(timezone.utc).strftime("%B %d, %Y")
    inner = """
  <div style="padding:28px;">
    <p style="font-size:15px;color:#333;margin:0 0 16px 0;line-height:1.55;">
      No significant FOG industry M&amp;A activity or news in the last 24 hours.
    </p>
    <p style="font-size:14px;color:#555;margin:0 0 16px 0;line-height:1.55;">
      The Command Center continues to monitor 10 industry categories daily and
      will alert you when material activity occurs.
    </p>
  </div>"""
    return _shell(f"Daily Briefing — {today} (Quiet Day)", inner, refreshed_ts)


def build_maintenance_html(refreshed_ts: str | None) -> str:
    """Fallback body sent when pre-flight validation drops so much
    content that the briefing would look broken. The recipient still
    knows the pipeline ran; we don't ship visibly half-rendered output."""
    today = datetime.now(timezone.utc).strftime("%B %d, %Y")
    inner = """
  <div style="padding:28px;">
    <p style="font-size:15px;color:#333;margin:0 0 16px 0;line-height:1.55;">
      The Command Center is undergoing a routine data-quality check today.
    </p>
    <p style="font-size:14px;color:#555;margin:0 0 16px 0;line-height:1.55;">
      Today's briefing is suppressed pending review. Tomorrow's regularly
      scheduled briefing will resume automatically.
    </p>
  </div>"""
    return _shell(f"Daily Briefing — {today} (System Maintenance)", inner, refreshed_ts)


def build_digest_html(new_today: list[dict], recent: list[dict],
                       new_deals: list[dict], refreshed_ts: str | None,
                       title_suffix: str = "") -> str:
    """Build the standard digest body. NEW TODAY shows items truly fresh
    in the last 24-48h; RECENT UPDATES shows 2-7-day-old items that the
    model just surfaced. Backfill (>7 days) is never rendered here."""
    today = datetime.now(timezone.utc).strftime("%B %d, %Y")
    deals_section = render_deals_table(new_deals)
    tier2_section = render_tier2_alerts(new_today + recent, new_deals)
    news_section = render_news_list(new_today, heading="NEW TODAY")
    recent_section = render_news_list(
        recent,
        heading="RECENT UPDATES — Newly indexed, past week",
        heading_color="#95a5a6",
    )

    # Lead text. Two flavors: standard ("X new news + Y deals today")
    # and recent-only ("no truly fresh news, here's what we surfaced").
    if not new_today and not new_deals and recent:
        summary_line = (
            "No new industry events in the last 24 hours. Below are recent "
            "updates from the past week newly indexed in the Command Center."
        )
    else:
        today_count = len(new_today)
        parts = [
            f"<b>{today_count}</b> new news item{'s' if today_count != 1 else ''} "
            f"and <b>{len(new_deals)}</b> deal{'s' if len(new_deals) != 1 else ''} today"
        ]
        if recent:
            parts.append(
                f"plus <b>{len(recent)}</b> recent update{'s' if len(recent) != 1 else ''} "
                f"from the past week"
            )
        summary_line = "Added " + ", ".join(parts) + "."

    inner = f"""
  <div style="padding:20px 28px;">
    <p style="font-size:14px;color:#444;margin:0 0 8px 0;">{summary_line}</p>
    {tier2_section}
    {deals_section}
    {news_section}
    {recent_section}
  </div>"""
    return _shell(f"Daily Briefing — {today}{title_suffix}", inner, refreshed_ts)


# ============================================================================
# Send orchestration
# ============================================================================

def _parse_recipients(raw: str | None) -> tuple[str | None, list[str]]:
    """Parse a comma-separated RECIPIENT_EMAIL into (primary, bcc_list).
    The first address is the owner / primary 'To'; everything else is
    BCC'd so external recipients never see each other's addresses."""
    if not raw:
        return None, []
    addrs = [a.strip() for a in raw.split(",") if a.strip()]
    if not addrs:
        return None, []
    return addrs[0], addrs[1:]


def _build_subject(today_str: str, new_deals: list[dict],
                    new_today_count: int, recent_count: int) -> str:
    """Subject-line strategy:
      - 0 new today + 0 recent + 0 deals  → '(Quiet Day)' — nothing happened
      - 1+ deals                          → '(N New Deals)' — deals win the suffix
      - 0 new today + 0 deals + 1+ recent → '(Recent Updates)' — defensive
                                            label since the NEW TODAY
                                            section will be empty
      - 1+ new today (no deals)           → no suffix — standard briefing
    """
    if new_today_count == 0 and recent_count == 0 and not new_deals:
        return f"FOG Industry Briefing — {today_str} (Quiet Day)"
    if new_deals:
        n = len(new_deals)
        return f"FOG Industry Briefing — {today_str} ({n} New Deal{'s' if n != 1 else ''})"
    if new_today_count == 0 and recent_count > 0:
        return f"FOG Industry Briefing — {today_str} (Recent Updates)"
    return f"FOG Industry Briefing — {today_str}"


def main() -> int:
    if not os.path.exists(LAST_REFRESH_PATH):
        sys.stderr.write(f"no {LAST_REFRESH_PATH} — refresh_data.py hasn't run yet; nothing to send\n")
        return 0
    with open(LAST_REFRESH_PATH, encoding="utf-8") as f:
        summary = json.load(f)

    refreshed_ts = summary.get("timestamp")

    # Pre-flight invariant: every deal added today must have a news
    # article. With refresh_data.py's sync invariant in place this is a
    # defensive no-op on almost every run; we keep it because the email
    # is the last layer before external distribution and we'd rather
    # fix-forward than ship inconsistent content.
    synth_count = preflight_deal_news_sync(summary)
    if synth_count:
        sys.stderr.write(f"  preflight: synthesized {synth_count} news entry(ies) to maintain deal/news sync\n")

    # Read the three news buckets refresh_data.py now writes. Legacy
    # last_refresh.json without the recent_news key falls back to the
    # previous two-bucket behavior.
    new_today_raw = summary.get("new_today_news") or []
    recent_raw = summary.get("recent_news") or []
    backfill_raw = summary.get("backfill_news") or []
    new_deals_raw = summary.get("new_deals") or []
    # Backward compat: if the summary predates the three-bucket split
    # AND has no explicit fresh/recent fields, drop everything from the
    # legacy combined `new_news` into new_today.
    if not new_today_raw and not recent_raw and summary.get("new_news"):
        new_today_raw = list(summary["new_news"])

    # Pre-flight: URL check across every candidate article (we don't
    # want to link to a broken URL even from RECENT UPDATES); then
    # per-item content validation. Backfill is excluded from the URL
    # check — those items don't appear in the email anyway. Bad items
    # are dropped quietly; we fall back to the maintenance template
    # only if validation would gut the briefing.
    pre_news_total = len(new_today_raw) + len(recent_raw)
    dead = precheck_urls(new_today_raw + recent_raw)
    new_today = validate_news_items(new_today_raw, dead)
    recent = validate_news_items(recent_raw, dead)
    new_deals = validate_deals(new_deals_raw)
    dropped = pre_news_total - (len(new_today) + len(recent))
    deals_dropped = len(new_deals_raw) - len(new_deals)

    fall_back_to_maintenance = False
    if pre_news_total > 0 and dropped >= max(2, (pre_news_total + 1) // 2):
        sys.stderr.write(
            f"pre-flight gutted the briefing ({dropped}/{pre_news_total} news dropped) — "
            f"falling back to maintenance template.\n"
        )
        fall_back_to_maintenance = True
    if deals_dropped and len(new_deals) == 0 and len(new_deals_raw) > 0:
        sys.stderr.write(
            "all deals failed validation — falling back to maintenance template.\n"
        )
        fall_back_to_maintenance = True

    # Quiet-day decision now includes recent_news in the "is anything
    # worth sending" check: a day with zero truly-fresh news but a
    # batch of recent updates is NOT a quiet day — we send the digest
    # with a Recent-Updates subject + lead. Only a day with all three
    # empty triggers the short quiet-day template.
    has_any_news = bool(new_today or recent)
    actionable = len(new_today) + len(new_deals) + len(recent)

    # Credentials + recipient parsing.
    gmail_addr = os.environ.get("GMAIL_ADDRESS")
    gmail_pw = os.environ.get("GMAIL_APP_PASSWORD")
    raw_recipients = os.environ.get("RECIPIENT_EMAIL")
    primary, bcc = _parse_recipients(raw_recipients)
    missing = []
    if not gmail_addr: missing.append("GMAIL_ADDRESS")
    if not gmail_pw:   missing.append("GMAIL_APP_PASSWORD")
    if not primary:    missing.append("RECIPIENT_EMAIL")
    if missing:
        sys.stderr.write(f"missing env: {', '.join(missing)} — cannot send email\n")
        return 1

    today_str = datetime.now(timezone.utc).strftime("%B %d, %Y")
    subject = _build_subject(today_str, new_deals, len(new_today), len(recent))

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((FROM_DISPLAY_NAME, gmail_addr))
    msg["To"] = primary
    if bcc:
        # Bcc is recognized by EmailMessage; smtplib's send_message()
        # strips it from the wire headers but still uses it for routing,
        # so external recipients never see one another's addresses.
        msg["Bcc"] = ", ".join(bcc)

    plain_intro = (
        "FOG Industry Briefing — view the HTML version for the formatted "
        f"daily briefing, or visit {SITE_URL}\n\n"
        "To unsubscribe, reply with UNSUBSCRIBE in the subject line."
    )
    msg.set_content(plain_intro)

    if fall_back_to_maintenance:
        body_html = build_maintenance_html(refreshed_ts)
        mode = "maintenance"
    elif actionable == 0:
        body_html = build_quiet_html(refreshed_ts)
        mode = "quiet"
    else:
        body_html = build_digest_html(new_today, recent, new_deals, refreshed_ts)
        mode = "digest"

    msg.add_alternative(body_html, subtype="html")

    print(f"Sending {mode} → To: {primary}, Bcc: {len(bcc)} recipient(s) | "
          f"subject: {subject!r}")
    print(f"  content: {len(new_today)} new today / {len(recent)} recent / "
          f"{len(backfill_raw)} backfill (suppressed from email) / "
          f"{len(new_deals)} deals (validation dropped: "
          f"{dropped} news, {deals_dropped} deals)")

    ctx = ssl.create_default_context()
    with smtplib.SMTP("smtp.gmail.com", 587) as s:
        s.starttls(context=ctx)
        s.login(gmail_addr, gmail_pw)
        s.send_message(msg)
    print("Sent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
