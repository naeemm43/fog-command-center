#!/usr/bin/env python3
"""Daily refresh for the FOG Industry Command Center.

Pipeline:
    1. Web-search via Anthropic API for recent FOG / liquid-waste industry
       news and M&A activity.
    2. Merge into data/news_feed.json (dedupe by normalized headline).
    3. Promote any M&A items into data/comp_database.json (dedupe by
       target+acquirer pair).
    4. Flag any deal whose location falls within 50 miles of a Tier 2
       target market.
    5. Splice the updated JSON arrays back into docs/index.html between
       the marker comments. The map's FOG_DATA / WWTP_DATA blocks are
       NEVER touched.
    6. Archive news older than 180 days to data/news_archive.json so the
       served HTML does not bloat indefinitely.

Run via the GitHub Actions workflow at .github/workflows/refresh.yml,
which sets ANTHROPIC_API_KEY from a repo secret. Manual local run:

    ANTHROPIC_API_KEY=sk-ant-... python scripts/refresh_data.py
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NEWS_PATH = os.path.join(ROOT, "data", "news_feed.json")
COMPS_PATH = os.path.join(ROOT, "data", "comp_database.json")
ARCHIVE_PATH = os.path.join(ROOT, "data", "news_archive.json")
HTML_PATH = os.path.join(ROOT, "docs", "index.html")

ARCHIVE_AFTER_DAYS = 180

TARGET_MARKETS: dict[str, tuple[float, float]] = {
    "Indianapolis, IN": (39.768, -86.158),
    "Columbus, OH": (39.961, -82.999),
    "Cincinnati, OH": (39.103, -84.512),
    "Minneapolis, MN": (44.978, -93.265),
    "Denver, CO": (39.739, -104.990),
    "Louisville, KY": (38.253, -85.758),
    "Salt Lake City, UT": (40.761, -111.891),
    "Omaha, NE": (41.257, -95.995),
    "Corpus Christi, TX": (27.801, -97.396),
    "Lubbock, TX": (33.549, -101.846),
    "El Paso, TX": (31.762, -106.485),
    "Cleveland, OH": (41.499, -81.694),
    "Detroit, MI": (42.331, -83.046),
    "Grand Rapids, MI": (42.963, -85.668),
}
TARGET_RADIUS_MI = 50.0

# US city → (lat, lon) lookup for deal locations. Kept small; if a deal
# location is not in this table we skip the proximity check rather than
# guess. Add cities here as the comp database grows.
CITY_COORDS: dict[str, tuple[float, float]] = {
    "indianapolis, in": (39.768, -86.158),
    "columbus, oh": (39.961, -82.999),
    "cincinnati, oh": (39.103, -84.512),
    "cleveland, oh": (41.499, -81.694),
    "minneapolis, mn": (44.978, -93.265),
    "denver, co": (39.739, -104.990),
    "louisville, ky": (38.253, -85.758),
    "salt lake city, ut": (40.761, -111.891),
    "omaha, ne": (41.257, -95.995),
    "corpus christi, tx": (27.801, -97.396),
    "lubbock, tx": (33.549, -101.846),
    "el paso, tx": (31.762, -106.485),
    "detroit, mi": (42.331, -83.046),
    "grand rapids, mi": (42.963, -85.668),
    "memphis, tn": (35.149, -90.049),
    "boone, nc": (36.216, -81.674),
    "portland, me": (43.659, -70.255),
    "new orleans, la": (29.951, -90.071),
    "st. louis, mo": (38.627, -90.199),
    "saint louis, mo": (38.627, -90.199),
    "new york, ny": (40.713, -74.006),
}


# ----------------------------- helpers --------------------------------


def haversine_miles(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * 3958.8 * math.asin(math.sqrt(h))


def coords_for_location(loc: str | None) -> tuple[float, float] | None:
    if not loc:
        return None
    key = loc.strip().lower()
    if key in CITY_COORDS:
        return CITY_COORDS[key]
    # try last comma-segment as "city, ST"
    if "," in loc:
        # e.g. "Suburban Boston, MA" → "boston, ma"
        m = re.search(r"([A-Za-z .'-]+),\s*([A-Z]{2})\b", loc)
        if m:
            k2 = (m.group(1).split()[-1] + ", " + m.group(2)).lower()
            if k2 in CITY_COORDS:
                return CITY_COORDS[k2]
    return None


def nearest_target_market(loc: str | None) -> tuple[str, float] | None:
    coord = coords_for_location(loc)
    if coord is None:
        return None
    best = None
    for name, mkt in TARGET_MARKETS.items():
        d = haversine_miles(coord, mkt)
        if best is None or d < best[1]:
            best = (name, d)
    return best


def normalize_headline(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def headline_overlap(a: str, b: str) -> float:
    """Token Jaccard between two normalized headlines."""
    sa = set(normalize_headline(a).split())
    sb = set(normalize_headline(b).split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def is_duplicate_news(item: dict, existing: list[dict]) -> bool:
    h = item.get("headline", "")
    norm = normalize_headline(h)
    if not norm:
        return False
    for e in existing:
        if normalize_headline(e.get("headline", "")) == norm:
            return True
        if headline_overlap(h, e.get("headline", "")) >= 0.8:
            return True
    return False


def is_duplicate_deal(item: dict, existing: list[dict]) -> bool:
    target = (item.get("target") or "").strip().lower()
    acq = (item.get("acquirer") or "").strip().lower()
    if not target or not acq:
        return True  # incomplete — skip
    for e in existing:
        et = (e.get("target") or "").strip().lower()
        ea = (e.get("acquirer") or "").strip().lower()
        if et == target and ea == acq:
            return True
    return False


# ----------------------------- web search -----------------------------

# Daily refresh queries — broader than the original narrow "grease trap" /
# "Wind River" / "LES" set so we catch deals that use different terminology
# or involve smaller / regional players.
EXPANDED_SEARCH_QUERIES = [
    # Broader industry terms
    '"liquid waste" acquired OR acquisition OR merger',
    '"waste recycling" acquired OR acquisition',
    '"septic" acquired OR acquisition OR sold',
    '"grease" company acquired OR sold OR merger',
    '"wastewater" company acquired OR acquisition',
    '"environmental services" acquisition OR merger',
    '"pumping" company acquired OR sold',
    '"drain cleaning" acquired OR acquisition',
    '"portable sanitation" acquisition OR merger',

    # Industry-specific publications and sources
    'site:wastetodaymagazine.com acquisition',
    'site:waste360.com acquisition merger',
    'site:pehub.com grease OR septic OR "liquid waste" OR wastewater',
    'site:privsource.com environmental services',
    'site:businesswire.com "liquid waste" OR "grease trap" OR "septic" acquired',

    # Named players we should be monitoring (beyond the Big 4)
    '"LJP Waste Solutions" acquisition',
    '"Southwaste" acquisition',
    '"Action Environmental" acquisition',
    '"Mr. Rooter" OR "Roto-Rooter" grease acquisition',
    '"National Waste Management" liquid waste',
    '"Synagro" acquisition',
    '"Clean Harbors" liquid waste',
    '"US Ecology" OR "Republic Services" liquid waste',
    '"Waste Connections" liquid waste acquisition',

    # Deal announcement patterns
    '"pleased to announce the acquisition of" grease OR septic OR "liquid waste" OR wastewater OR pumping',
    '"has been acquired by" grease OR septic OR "liquid waste" OR wastewater',
    '"announces sale of" septic OR "liquid waste" OR wastewater OR grease OR pumping',
    '"private equity" "environmental services" "add-on" OR "platform"',

    # Regional searches for under-covered markets
    '"liquid waste" acquisition midwest OR texas OR southeast',
    'grease trap company sold OR acquired {year}',
]

# One-time historical sweep run by scripts/catchup.py.
CATCHUP_QUERIES = [
    '"United Liquid Waste Recycling" "LJP Waste Solutions"',
    '"liquid waste" acquisition 2024',
    '"liquid waste" acquisition 2023',
    '"grease trap" company sold 2024',
    '"grease trap" company sold 2023',
    '"septic company" acquired 2024',
    '"septic company" acquired 2023',
    '"wastewater services" acquired 2024 2025',
    'site:privsource.com "liquid waste" OR "grease" OR "septic" OR "wastewater"',
    'site:pehub.com "liquid waste" OR "grease trap" OR septic OR wastewater acquisition 2024 2025 2026',
    'site:axial.net "liquid waste" OR grease OR septic OR wastewater',
    '"environmental services" "add-on acquisition" 2024 2025 2026',
    '"non-hazardous" waste acquisition private equity',
    '"waste management" acquisition "grease trap" OR "liquid waste" OR septic',
]


def _build_search_prompt(today: str, year: int, queries: list[str], window_note: str) -> str:
    enumerated = "\n".join(f"{i+1}. {q.format(year=year)}" for i, q in enumerate(queries))
    return f"""Today is {today}. Search the web for news and M&A activity in the non-hazardous liquid waste, grease trap, FOG (fats oils grease), septic, wastewater services, and environmental services industry in the United States.

Search ALL of the following queries (use the web_search tool, one query at a time):
{enumerated}

{window_note}

For each relevant result, return a JSON object with these fields:
- date (YYYY-MM-DD format; if only month is known, use the 15th)
- headline (article title)
- source (publication name)
- source_url (URL)
- category (one of: "M&A", "Regulation", "Market", "Company News")
- summary (2-3 sentence summary)
- is_deal (true if this is an M&A transaction announcement)
- buyer (if M&A deal, the acquiring entity)
- target (if M&A deal, the acquired entity)
- sponsor (if PE-backed buyer, the sponsor name)
- location (city, state if identifiable)
- deal_size (e.g. "$120M EV", or "Undisclosed")
- multiple (EV/EBITDA if disclosed or inferrable, else "N/A")
- deal_summary (array of 1-2 short bullets with context: customer count, fleet, route density, retention, etc.)
- owner_classification (one of: "PE-Backed", "Public Company", "Family/Local", "Regional", "Municipal", "Unknown")

Return ONLY a JSON array of result objects. No surrounding prose. Skip generic industry overviews — focus on specific deals, announcements, regulatory actions, and concrete company news. If a query returns nothing relevant, omit it from the output."""


def search_for_updates(queries: list[str] | None = None,
                        window_note: str | None = None) -> list[dict]:
    """Ask Claude (with web_search tool) for industry news.

    queries:     list of search strings; defaults to EXPANDED_SEARCH_QUERIES.
    window_note: instruction describing the time window of interest. Defaults
                 to "last 30 days" for daily refresh.
    """
    try:
        import anthropic
    except ImportError:
        sys.stderr.write("anthropic package not installed; skipping web search\n")
        return []

    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.stderr.write("ANTHROPIC_API_KEY not set; skipping web search\n")
        return []

    client = anthropic.Anthropic()
    today = datetime.now().strftime("%Y-%m-%d")
    year = datetime.now().year
    qs = queries if queries is not None else EXPANDED_SEARCH_QUERIES
    note = window_note or "Focus on items from the last 30 days."

    prompt = _build_search_prompt(today, year, qs, note)

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=8192,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role": "user", "content": prompt}],
    )

    text_parts = [b.text for b in response.content if hasattr(b, "text")]
    full_text = "\n".join(text_parts)
    m = re.search(r"\[.*\]", full_text, re.DOTALL)
    if not m:
        sys.stderr.write("no JSON array found in model response\n")
        return []
    try:
        return json.loads(m.group())
    except json.JSONDecodeError as e:
        sys.stderr.write(f"could not parse model JSON: {e}\n")
        return []


# ----------------------------- merging --------------------------------


def coerce_news_item(raw: dict) -> dict | None:
    """Normalize a search result into the news_feed.json schema."""
    if not raw.get("headline") or not raw.get("date"):
        return None
    cat = raw.get("category") or "Company News"
    if cat not in {"M&A", "Regulation", "Market", "Company News"}:
        cat = "Company News"
    item: dict = {
        "date": raw["date"],
        "headline": raw["headline"],
        "source": raw.get("source", ""),
        "source_url": raw.get("source_url", ""),
        "category": cat,
        "summary": raw.get("summary", ""),
        "is_target_market": False,
        "target_market_name": None,
    }
    near = nearest_target_market(raw.get("location"))
    if near and near[1] <= TARGET_RADIUS_MI:
        item["is_target_market"] = True
        item["target_market_name"] = f"{near[0]} ({near[1]:.0f} mi)"
    return item


def _normalize_summary_bullets(s) -> list[str] | None:
    if s is None:
        return None
    if isinstance(s, list):
        out = [str(x).strip() for x in s if str(x).strip()]
        return out or None
    text = str(s).strip()
    if not text:
        return None
    # If model returned newline-separated bullets, split them.
    parts = [p.strip(" -*•\t").strip() for p in text.splitlines() if p.strip()]
    if len(parts) > 1:
        return parts
    return [text]


def coerce_deal_from_news(raw: dict) -> dict | None:
    if not raw.get("is_deal"):
        return None
    if not raw.get("buyer") or not raw.get("target"):
        return None
    deal: dict = {
        "date": raw.get("date", ""),
        "target": raw["target"],
        "acquirer": raw["buyer"],
        "sponsor": raw.get("sponsor", ""),
        "location": raw.get("location", ""),
        "deal_type": raw.get("deal_type", "Add-On"),
        "deal_size": raw.get("deal_size", "Undisclosed"),
        "multiple": raw.get("multiple") or "N/A",
        "services": raw.get("services", ""),
        "source": raw.get("source", ""),
        "source_url": raw.get("source_url", ""),
        "is_target_market": False,
        "target_market_name": None,
        "owner_classification": raw.get("owner_classification") or "Unknown",
        "deal_summary": _normalize_summary_bullets(raw.get("deal_summary")),
        "notes": raw.get("summary", ""),
    }
    near = nearest_target_market(raw.get("location"))
    if near and near[1] <= TARGET_RADIUS_MI:
        deal["is_target_market"] = True
        deal["target_market_name"] = f"{near[0]} ({near[1]:.0f} mi)"
    return deal


# ----------------------------- archiving ------------------------------


def split_news_by_age(news: list[dict]) -> tuple[list[dict], list[dict]]:
    """Return (kept, archived). Items older than ARCHIVE_AFTER_DAYS go to
    the archive list."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=ARCHIVE_AFTER_DAYS)
    kept, archived = [], []
    for n in news:
        try:
            dt = datetime.fromisoformat(n["date"]).replace(tzinfo=timezone.utc)
        except (KeyError, ValueError):
            kept.append(n)
            continue
        (kept if dt >= cutoff else archived).append(n)
    return kept, archived


# ----------------------------- HTML splice ----------------------------


def splice_block(html: str, start: str, end: str, new_inner: str) -> str:
    """Replace the inner text between `<!-- start -->` and `<!-- end -->`
    markers in `html`."""
    pattern = re.compile(
        r"(<!--\s*" + re.escape(start) + r"\s*-->)(.*?)(<!--\s*" + re.escape(end) + r"\s*-->)",
        re.DOTALL,
    )
    if not pattern.search(html):
        sys.stderr.write(f"WARNING: marker pair {start}/{end} not found in HTML\n")
        return html
    return pattern.sub(lambda m: m.group(1) + "\n" + new_inner + "\n" + m.group(3), html)


def update_html(news: list[dict], comps: list[dict], metadata: dict) -> bool:
    if not os.path.exists(HTML_PATH):
        sys.stderr.write(f"docs/index.html not found at {HTML_PATH}; skipping HTML splice\n")
        return False
    with open(HTML_PATH, encoding="utf-8") as f:
        html = f.read()

    html = splice_block(
        html, "NEWS_FEED_DATA_START", "NEWS_FEED_DATA_END",
        "<script>\nconst newsFeedData = " + json.dumps(news, indent=2) + ";\n</script>",
    )
    html = splice_block(
        html, "COMP_DATABASE_DATA_START", "COMP_DATABASE_DATA_END",
        "<script>\nconst compDatabaseData = " + json.dumps(comps, indent=2) + ";\n</script>",
    )
    html = splice_block(
        html, "METADATA_START", "METADATA_END",
        "<script>\nconst metadata = " + json.dumps(metadata, indent=2) + ";\n</script>",
    )

    with open(HTML_PATH, "w", encoding="utf-8") as f:
        f.write(html)
    return True


# ----------------------------- entrypoint -----------------------------


def main() -> int:
    with open(NEWS_PATH, encoding="utf-8") as f:
        news = json.load(f)
    with open(COMPS_PATH, encoding="utf-8") as f:
        comps = json.load(f)

    raw_results = search_for_updates()
    print(f"Search returned {len(raw_results)} raw results")

    added_news = 0
    added_deals = 0
    for r in raw_results:
        n = coerce_news_item(r)
        if n and not is_duplicate_news(n, news):
            news.append(n)
            added_news += 1
        d = coerce_deal_from_news(r)
        if d and not is_duplicate_deal(d, comps):
            comps.append(d)
            added_deals += 1

    # sort newest-first
    news.sort(key=lambda x: x.get("date", ""), reverse=True)
    comps.sort(key=lambda x: x.get("date", ""), reverse=True)

    # archive old news
    news, archived = split_news_by_age(news)
    if archived:
        existing_archive: list[dict] = []
        if os.path.exists(ARCHIVE_PATH):
            with open(ARCHIVE_PATH, encoding="utf-8") as f:
                existing_archive = json.load(f)
        # de-dupe archive by headline
        existing_keys = {normalize_headline(a.get("headline", "")) for a in existing_archive}
        for a in archived:
            if normalize_headline(a.get("headline", "")) not in existing_keys:
                existing_archive.append(a)
        existing_archive.sort(key=lambda x: x.get("date", ""), reverse=True)
        with open(ARCHIVE_PATH, "w", encoding="utf-8") as f:
            json.dump(existing_archive, f, indent=2)

    with open(NEWS_PATH, "w", encoding="utf-8") as f:
        json.dump(news, f, indent=2)
    with open(COMPS_PATH, "w", encoding="utf-8") as f:
        json.dump(comps, f, indent=2)

    metadata = {
        "lastRefreshed": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "newsCount": len(news),
        "compCount": len(comps),
    }
    update_html(news, comps, metadata)

    print(
        f"Refresh complete. Added {added_news} news items, {added_deals} deals. "
        f"Total: {len(news)} news (+{len(archived)} archived), {len(comps)} deals."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
