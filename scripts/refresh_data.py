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

# Hard cap on how old an inbound article can be before we drop it. The
# search prompt asks the model for last-7-day items only, but the model
# regularly surfaces landmark headlines from months/years ago that rank
# high in web search results. Anything older than this is silently
# rejected by coerce_news_item / coerce_deal_from_news so the daily
# digest stays a true "what happened in the last week" view rather than
# an evergreen-headlines tracker.
MAX_AGE_DAYS = 14

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


# ============================================================================
# Deal-location geocoding (for the "Find on Map" button on transaction rows).
# Stored coords are (lat, lng) in WGS84.  zoom_hint is "narrow" for specific
# city locations (Leaflet zoom level ~12) or "wide" for multi-region /
# state-only descriptors (zoom level ~5-6).
# ============================================================================

DEAL_CITY_COORDS: dict[str, tuple[float, float]] = {
    "Irving, TX": (32.814, -96.949),
    "Marlborough, MA": (42.346, -71.552),
    "Stuart, FL": (27.198, -80.253),
    "Mahopac, NY": (41.372, -73.731),
    "Tampa Bay, FL": (27.951, -82.459),
    "Tampa, FL": (27.951, -82.459),
    "Central Florida": (28.538, -81.379),
    "South Florida": (26.122, -80.137),
    "Tallahassee, FL": (30.438, -84.281),
    "Charlotte, NC": (35.227, -80.843),
    "Boone, NC": (36.217, -81.674),
    "North Carolina": (35.630, -79.806),
    "Quinton, VA": (37.525, -77.225),
    "Arlington, TN": (35.296, -89.661),
    "Memphis, TN": (35.149, -90.049),
    "Washington, PA": (40.174, -80.246),
    "Loretto, PA": (40.503, -78.632),
    "Honesdale, PA": (41.577, -75.259),
    "Matamoras, PA": (41.369, -74.700),
    "Greentown, PA": (41.326, -75.275),
    "Crofton, MD": (39.018, -76.687),
    "St. Louis, MO": (38.627, -90.199),
    "New Orleans, LA": (29.951, -90.072),
    "New York, NY": (40.713, -74.006),
    "Portland, ME": (43.661, -70.255),
    "Atlanta, GA": (33.749, -84.388),
    "Fayetteville, GA": (33.449, -84.455),
    "Elgin, IL": (42.037, -88.281),
    "New England": (42.407, -71.383),
    "Florida": (28.538, -81.379),
    "Pennsylvania": (40.876, -77.822),
    "Georgia": (33.247, -83.441),
    "West Virginia": (38.598, -80.454),
    "Connecticut": (41.603, -73.087),
    "Maine": (45.254, -69.445),
    "California": (36.778, -119.418),
    "Massachusetts": (42.407, -71.383),
    "Texas": (31.000, -100.000),
    "Maryland": (39.045, -76.641),
    "Virginia": (37.432, -78.657),
    "New Jersey": (40.058, -74.405),
    "Delaware": (38.910, -75.527),
    "Vermont": (44.000, -72.700),
    "New Hampshire": (43.193, -71.572),
    "Kentucky": (37.840, -84.270),
    "Tennessee": (35.860, -86.660),
    "Southeast US": (33.749, -84.388),
    "Northeast US": (41.203, -73.201),
    "Midwest": (41.881, -87.629),
    "National": (39.828, -98.580),
    "Multi-state": (39.828, -98.580),
    "Mid-Atlantic": (39.045, -76.641),
    "Upper Midwest": (44.0, -89.5),
}

# Full state name → 2-letter abbreviation, for normalizing "Atlanta, Georgia"
# → "Atlanta, GA" before lookup.
_STATE_ABBREV: dict[str, str] = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE",
    "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}

# State-level fallback centers, used when we can identify a state but not
# the city. Matches the wide-zoom layer.
_STATE_CENTER: dict[str, tuple[float, float]] = {
    "AL": (32.806, -86.791), "AK": (61.371, -152.404), "AZ": (33.730, -111.431),
    "AR": (34.969, -92.373), "CA": (36.778, -119.418), "CO": (39.060, -105.311),
    "CT": (41.603, -73.087), "DE": (38.910, -75.527), "FL": (28.538, -81.379),
    "GA": (33.247, -83.441), "HI": (20.792, -156.331), "ID": (44.240, -114.479),
    "IL": (40.349, -88.986), "IN": (39.849, -86.258), "IA": (42.011, -93.210),
    "KS": (38.526, -96.726), "KY": (37.840, -84.270), "LA": (31.169, -91.867),
    "ME": (45.254, -69.445), "MD": (39.045, -76.641), "MA": (42.407, -71.383),
    "MI": (44.314, -85.602), "MN": (45.694, -93.900), "MS": (32.741, -89.678),
    "MO": (38.456, -92.288), "MT": (46.921, -110.454), "NE": (41.125, -98.268),
    "NV": (38.313, -117.055), "NH": (43.452, -71.563), "NJ": (40.058, -74.405),
    "NM": (34.840, -106.248), "NY": (40.713, -74.006), "NC": (35.630, -79.806),
    "ND": (47.528, -99.784), "OH": (40.388, -82.764), "OK": (35.565, -96.928),
    "OR": (44.572, -122.071), "PA": (40.876, -77.822), "RI": (41.680, -71.512),
    "SC": (33.856, -80.945), "SD": (44.299, -99.439), "TN": (35.860, -86.660),
    "TX": (31.000, -100.000), "UT": (40.150, -111.862), "VT": (44.045, -72.711),
    "VA": (37.769, -78.170), "WA": (47.400, -121.490), "WV": (38.491, -80.954),
    "WI": (44.268, -89.616), "WY": (42.756, -107.302),
}

# Additional commonly-seen city/town coords beyond the user-provided table.
_EXTRA_CITY_COORDS: dict[str, tuple[float, float]] = {
    "Bayville, NJ": (39.901, -74.150),
    "Cherryville, NC": (35.388, -81.379),
    "Clearwater, FL": (27.965, -82.800),
    "Hiram, GA": (33.875, -84.764),
    "Holbrook, MA": (42.156, -71.005),
    "Ivyland, PA": (40.211, -75.067),
    "Kennett Square, PA": (39.846, -75.711),
    "Lake Hopatcong, NJ": (40.964, -74.610),
    "Largo, FL": (27.910, -82.787),
    "Orlando, FL": (28.538, -81.379),
    "St. Cloud, FL": (28.249, -81.281),
    "Saint Cloud, FL": (28.249, -81.281),
    "Temple, PA": (40.404, -75.926),
    "Urbanna, VA": (37.638, -76.574),
    "Westville, NJ": (39.866, -75.131),
    "Beloit, WI": (42.508, -89.032),
    "Hueytown, AL": (33.453, -86.998),
    "Chicago, IL": (41.878, -87.630),
    "San Diego, CA": (32.716, -117.161),
    "Houston, TX": (29.760, -95.370),
    "Dallas, TX": (32.776, -96.797),
    "Phoenix, AZ": (33.448, -112.074),
    "Seattle, WA": (47.606, -122.332),
    "Boston, MA": (42.360, -71.058),
    "Philadelphia, PA": (39.953, -75.165),
    "Richmond, VA": (37.541, -77.434),
    "Pittsburgh, PA": (40.441, -79.996),
    "Suburban Boston, MA": (42.360, -71.058),
}

_VAGUE_HINTS = (
    "/", "|", "multi-state", "multi state", "national", "nationwide",
    "northeast", "southeast", "midwest", "southwest", "northwest", "west coast",
    "east coast", "tri-state", "mid-atlantic", "new england", "upper midwest",
    "coastal", "southwestern",
)


def _is_vague_location(s: str) -> bool:
    s_low = s.lower()
    if any(h in s_low for h in _VAGUE_HINTS):
        return True
    # bare state name (no comma) → wide
    if "," not in s and s.strip() in DEAL_CITY_COORDS:
        only = s.strip()
        # heuristic: if it doesn't look like a city + state code
        return not re.match(r"^[A-Za-z .'-]+,\s*[A-Z]{2}$", only)
    return False


_CITY_STATE_RX = re.compile(r"([A-Za-z .'\-]+?),\s*([A-Z]{2})\b")


def _all_city_coords() -> dict[str, tuple[float, float]]:
    """Combine the user-provided and extra city dicts into one lookup."""
    out = dict(DEAL_CITY_COORDS)
    out.update(_EXTRA_CITY_COORDS)
    return out


def _normalize_state_in(s: str) -> str:
    """Replace 'City, Florida' with 'City, FL' so it matches our dicts."""
    parts = s.split(",")
    if len(parts) >= 2:
        tail = parts[-1].strip()
        if tail.lower() in _STATE_ABBREV:
            parts[-1] = " " + _STATE_ABBREV[tail.lower()]
            return ",".join(parts).strip()
    return s


def geocode_deal_location(loc: str | None) -> tuple[float, float, str] | None:
    """Return (lat, lng, zoom_hint) for a deal location string, or None
    if no coordinate can be confidently inferred. zoom_hint is 'narrow'
    (city zoom ~12) or 'wide' (multi-region zoom ~5)."""
    if not loc:
        return None
    s = loc.strip()
    if not s:
        return None
    cities = _all_city_coords()
    s_norm = _normalize_state_in(s)

    def _try(key: str, hint: str):
        if key in cities:
            lat, lng = cities[key]
            return (lat, lng, hint)
        return None

    hint = "wide" if _is_vague_location(s) else "narrow"
    r = _try(s_norm, hint) or _try(s, hint)
    if r:
        return r

    # Strip parens: "National (HQ Irving, TX)" — try inner THEN outer.
    paren = re.match(r"^(.*?)\s*\((.+?)\)\s*$", s)
    if paren:
        for cand in (paren.group(2).strip(), paren.group(1).strip()):
            if not cand:
                continue
            cand_norm = _normalize_state_in(cand)
            r = _try(cand_norm, "narrow") or _try(cand, "narrow")
            if r:
                return r

    # Multi-region separators: "Beloit, WI / Upper Midwest" → first chunk.
    for sep in ("/", "|", " and ", " & "):
        if sep in s:
            for chunk in s.split(sep):
                ch = _normalize_state_in(chunk.strip())
                r = _try(ch, "wide")
                if r:
                    return r

    # "City, ST" anywhere in the string.
    for src in (s_norm, s):
        m = _CITY_STATE_RX.search(src)
        if m:
            key = m.group(1).strip() + ", " + m.group(2)
            r = _try(key, "narrow")
            if r:
                return r
            last_word = m.group(1).strip().split()[-1] if m.group(1).strip() else ""
            if last_word:
                r = _try(last_word + ", " + m.group(2), "narrow")
                if r:
                    return r
            # Fall back to state center.
            st = m.group(2)
            if st in _STATE_CENTER:
                lat, lng = _STATE_CENTER[st]
                return (lat, lng, "wide")

    # Pure state name or abbreviation with no city — match longest first
    # so "New York" wins over "York" hidden inside it.
    for name in sorted(_STATE_ABBREV, key=len, reverse=True):
        if re.search(rf"\b{re.escape(name)}\b", s, re.IGNORECASE):
            st = _STATE_ABBREV[name]
            lat, lng = _STATE_CENTER[st]
            return (lat, lng, "wide")
    for word in re.findall(r"\b[A-Z]{2}\b", s):
        if word in _STATE_CENTER:
            lat, lng = _STATE_CENTER[word]
            return (lat, lng, "wide")

    return None


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


# Filler/stop words and time references stripped from headlines before
# the fuzzy-match overlap is computed. The goal is to make two press
# write-ups of the same story collapse onto the same normalized form
# even when one says "Acme Inc. announces Q1 2026 results" and the other
# says "Acme announces first-quarter results". Company-form suffixes are
# included so 'Acme Inc' and 'Acme Corp' don't differ on a single token.
_HEADLINE_STOPWORDS = {
    "the", "a", "an", "of", "in", "on", "at", "to", "for",
    "and", "or", "with", "by", "from", "as", "is", "are",
    "be", "was", "were", "this", "that", "these", "those",
    "its", "it", "into", "after", "before",
}
_HEADLINE_COMPANY_SUFFIXES = {
    "inc", "incorporated", "llc", "lc", "corp", "corporation",
    "ltd", "limited", "co", "company", "lp", "llp", "plc",
    "ag", "gmbh", "sa",
}
_HEADLINE_TIME_TOKENS_RX = re.compile(
    r"\b(?:20\d{2}|q[1-4]|fy20\d{2}|h[12])\b", re.IGNORECASE,
)


def normalize_headline(s: str) -> str:
    """Lowercase, strip punctuation, drop stop words / company suffixes /
    year+quarter tokens, collapse whitespace. Output is a space-joined
    string of "content" tokens used both for set-overlap comparisons and
    for exact-match equality checks."""
    s = (s or "").lower()
    s = _HEADLINE_TIME_TOKENS_RX.sub(" ", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    drop = _HEADLINE_STOPWORDS | _HEADLINE_COMPANY_SUFFIXES
    tokens = [t for t in s.split() if t and t not in drop]
    return " ".join(tokens)


_PROPER_NOUN_RX = re.compile(r"\b([A-Z][a-zA-Z0-9&]{2,}(?:\s+[A-Z][a-zA-Z0-9&]+)*)\b")


def _proper_noun_tokens(s: str) -> set[str]:
    """Extract capitalized multi/single-word tokens from a raw (non-
    lowercased) headline. Used as the third dedup axis: same publication
    date + 4+ shared proper nouns is a strong same-story signal even
    when the surrounding wording diverges.

    Drops both press-release boilerplate AND generic FOG-industry terms
    so we don't false-positive cluster (a) two same-day acquisitions by
    the same acquirer of different targets (Wind River acquires Mahopac
    AND R Crews on 2019-06-15) or (b) parallel regulation overview
    articles for different states published the same day."""
    if not s:
        return set()
    junk = {
        # Press-release boilerplate.
        "The", "A", "An", "And", "Or", "For", "On", "In", "At",
        "Q1", "Q2", "Q3", "Q4", "Inc", "LLC", "Corp", "Ltd", "Co",
        "Company", "Corporation", "Limited", "Incorporated",
        # Generic FOG-industry terms — capitalized in headlines but
        # carry zero disambiguation signal in this dataset.
        "Grease", "Trap", "Septic", "Environmental", "Wastewater",
        "Waste", "Industrial", "Solutions", "Services", "Service",
        "Acquires", "Announces", "Reports", "Acquisition", "Acquired",
        "Regulations", "Regulation", "Guide", "FOG", "UCO",
    }
    out: set[str] = set()
    for m in _PROPER_NOUN_RX.finditer(s):
        for tok in m.group(0).split():
            if tok not in junk and len(tok) >= 3:
                out.add(tok)
    return out


# Anthropic web_search responses sometimes wrap source-citation excerpts in
# <cite index="..."> ... </cite> XML tags (or the antml:cite variant). We
# don't want those rendered in the news feed. Strip every tag-like fragment
# but preserve inner text.
_TAG_RX = re.compile(r"</?[A-Za-z][^>]*>")


def clean_summary(text: str | None) -> str | None:
    if text is None:
        return None
    if not isinstance(text, str):
        return text
    cleaned = _TAG_RX.sub("", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def headline_overlap(a: str, b: str) -> float:
    """Token Jaccard between two normalized headlines."""
    sa = set(normalize_headline(a).split())
    sb = set(normalize_headline(b).split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


# ----------------------------- input validation -----------------------
#
# Two failure modes we've seen from web-search results:
#   1. The model returns a homepage URL (e.g. "https://pehub.com") instead
#      of the actual article URL. Reject these so they never become
#      sources.
#   2. The model uses a current-day date for an article that was actually
#      published years earlier (the "search crawl date" looks like the
#      publication date). When a result also includes a URL we can fetch
#      the article and validate the date matches.
#
# We can't fully verify dates without an extra HTTP call per result, but
# we can reject obviously-bad URLs and flag suspicious recent dates so the
# operator can review.


_HOMEPAGE_PATH_PATTERNS = (
    "",        # no path
    "/",       # root
    "/home",
    "/index",
    "/index.html",
    "/index.htm",
    "/news",   # generic listing
    "/news/",
    "/blog",
    "/blog/",
)


def looks_like_homepage(url: str) -> bool:
    """Return True if `url` is a homepage / listing page rather than an
    article. Used to reject placeholder source URLs at ingestion."""
    if not url:
        return True
    try:
        from urllib.parse import urlparse
        p = urlparse(url.strip())
    except Exception:
        return True
    if not p.netloc:
        return True
    path = (p.path or "").rstrip("/")
    # Strip trailing /index.html etc.
    return path.lower() in {x.rstrip("/") for x in _HOMEPAGE_PATH_PATTERNS}


def valid_iso_date(s: str) -> bool:
    if not isinstance(s, str) or len(s) < 8:
        return False
    try:
        datetime.fromisoformat(s)
        return True
    except ValueError:
        return False


def is_too_old(s: str, max_age_days: int = MAX_AGE_DAYS) -> bool:
    """True if the ISO date string is more than `max_age_days` before
    today (UTC). Used to drop landmark / evergreen articles that the
    model surfaces even when asked for last-week-only items."""
    if not s or not valid_iso_date(s):
        return False
    try:
        dt = datetime.fromisoformat(s).date()
    except ValueError:
        return False
    age = (datetime.now(timezone.utc).date() - dt).days
    return age > max_age_days


def is_future_date(s: str) -> bool:
    """True if the ISO date string is later than today (UTC).
    Used to flag the model occasionally extracting tomorrow's date or
    the search-index date instead of the actual publication date."""
    if not valid_iso_date(s):
        return False
    try:
        dt = datetime.fromisoformat(s).date()
    except ValueError:
        return False
    return dt > datetime.now(timezone.utc).date()


def _dates_within_30(date_a: str | None, date_b: str | None) -> bool:
    """True iff both ISO dates parse and their delta is <= 30 days. Used
    to gate the URL-match dedup arm: same URL with very distant dates is
    treated as a hallucinated URL, not a true duplicate."""
    if not date_a or not date_b:
        return False
    try:
        da = datetime.fromisoformat(date_a).date()
        db = datetime.fromisoformat(date_b).date()
    except (ValueError, TypeError):
        return False
    return abs((da - db).days) <= 30


def _normalize_url(u: str | None) -> str:
    """Strip query string + fragment + trailing slash + scheme so two
    URLs that differ only in tracking params dedupe to the same key."""
    if not u:
        return ""
    s = u.strip().lower()
    s = re.sub(r"^https?://", "", s)
    s = re.sub(r"^www\.", "", s)
    s = s.split("#", 1)[0].split("?", 1)[0]
    return s.rstrip("/")


def is_duplicate_news(item: dict, existing: list[dict]) -> bool:
    """Fuzzy duplicate check. Returns True if the incoming item matches
    any existing item by any of:

      1. Same canonical URL (scheme/www/query/fragment stripped).
      2. Normalized-headline word-overlap (Jaccard) >= 0.75.
      3. Same publication date AND >=3 shared proper-noun tokens
         in the raw headlines.

    `existing` should include both news_feed.json and news_archive.json
    so an article that already lived through one archival cycle doesn't
    get re-introduced."""
    h = item.get("headline") or ""
    norm = normalize_headline(h)
    if not norm:
        return False
    item_url = _normalize_url(item.get("source_url"))
    item_pn = _proper_noun_tokens(h)
    item_date = item.get("date")
    item_tokens = set(norm.split())

    for e in existing:
        e_norm = normalize_headline(e.get("headline") or "")
        if not e_norm:
            continue
        e_tokens = set(e_norm.split())

        # URL match is the strongest signal, BUT some upstream items
        # carry bogus URLs (model hallucinated the wrong slug, or the
        # URL is a bare homepage). To avoid collapsing unrelated stories
        # that share a hallucinated URL, require both minimal token
        # overlap AND date proximity before honoring the URL match.
        # Two stories with the same URL but five months apart and
        # different headlines are almost certainly a data-quality bug,
        # not the same article.
        e_url = _normalize_url(e.get("source_url"))
        if item_url and e_url and item_url == e_url and item_tokens and e_tokens:
            url_jaccard = len(item_tokens & e_tokens) / len(item_tokens | e_tokens)
            if url_jaccard >= 0.3 and _dates_within_30(item_date, e.get("date")):
                return True

        if e_norm == norm:
            return True

        if item_tokens and e_tokens:
            jaccard = len(item_tokens & e_tokens) / len(item_tokens | e_tokens)
            if jaccard >= 0.75:
                return True

        if (item_date and e.get("date") == item_date and item_pn):
            e_pn = _proper_noun_tokens(e.get("headline") or "")
            # Threshold 4 (not 3) because two same-day stories about
            # different deals by the same acquirer can share company
            # name tokens like {Wind, River, Environmental}. Requiring
            # one additional distinctive shared name forces real-story-
            # match rather than acquirer-match.
            if len(item_pn & e_pn) >= 4:
                return True

    return False


_COMPANY_SUFFIXES = {
    "inc", "incorporated",
    "llc", "lc",
    "corp", "corporation",
    "ltd", "limited",
    "co", "company",
    "lp", "llp",
    "plc",
    "ag", "gmbh", "sa",
    "the",
}

# Generic FOG-industry terms that appear in many company names without
# distinguishing them. Stripped during normalization so the overlap
# heuristic doesn't false-positive on "X Septic" + "Y Septic" — saw it
# merge Mahopac Septic + R Crews Family Septic on a single shared
# "septic" token (50% of the shorter set, tripping the loose threshold).
_INDUSTRY_GENERICS = {
    "septic",
    "service", "services",
    "environmental",
    "waste",
    "industrial",
    "solutions",
    "group", "groups",
    "holdings",
    "partners",
    "international",
    "enterprises", "enterprise",
}


def _normalize_company(s: str | None) -> str:
    """Lowercase, strip punctuation, drop entity-form suffixes + generic
    FOG industry words, collapse whitespace. Used by the fuzzy deal-dedupe
    so 'Acme Inc.', 'Acme, LLC', and 'Acme Corp' all reduce to 'acme',
    and so 'Acme Septic Services' and 'Bcme Septic Services' don't get
    treated as 50%-similar just because they both end in 'septic'."""
    if not s:
        return ""
    s = s.lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    drop = _COMPANY_SUFFIXES | _INDUSTRY_GENERICS
    tokens = [t for t in s.split() if t and t not in drop]
    return " ".join(tokens)


def _strings_match(a: str, b: str, threshold: float = 0.7) -> bool:
    """True if normalized strings overlap strongly. Used for both target
    and acquirer fields. Two checks: substring containment OR word-set
    overlap above the threshold."""
    if not a or not b:
        return False
    if a == b:
        return True
    if a in b or b in a:
        return True
    wa, wb = set(a.split()), set(b.split())
    if not wa or not wb:
        return False
    overlap = len(wa & wb) / min(len(wa), len(wb))
    return overlap >= threshold


def _dates_within(date_a: str | None, date_b: str | None, max_days: int) -> bool:
    """Both dates parse + delta is within max_days. Returns False if either
    is unparseable, so we never cluster on a phantom 1970-01-01 default."""
    if not date_a or not date_b:
        return False
    try:
        da = datetime.fromisoformat(date_a).date()
        db = datetime.fromisoformat(date_b).date()
    except (ValueError, TypeError):
        return False
    return abs((da - db).days) <= max_days


def _locations_within(a: dict, b: dict, max_miles: float) -> bool:
    """Both records have lat/lng and the great-circle distance is within
    max_miles. Used as a third-axis dup check when target strings diverge."""
    try:
        ax, ay = a.get("latitude"), a.get("longitude")
        bx, by = b.get("latitude"), b.get("longitude")
        if ax is None or ay is None or bx is None or by is None:
            return False
        return haversine_miles((float(ax), float(ay)), (float(bx), float(by))) <= max_miles
    except (TypeError, ValueError):
        return False


def is_duplicate_deal(item: dict, existing: list[dict]) -> bool:
    """Fuzzy match against an existing deal list. Two deals are considered
    duplicates if:

      (a) Acquirers match (substring or >=70% word overlap on normalized
          company strings), AND ONE OF:
            - Targets match strictly (>=80% overlap or substring) →
              cluster regardless of date. Catches rolling-announcement
              duplicates of the same deal across weeks/months.
            - Targets match loosely (>=50% overlap) AND dates within
              60 days. Catches small naming-variant differences.
            - Locations within 50 miles AND dates within 60 days.
              Catches cases where the target string is rewritten
              entirely but the deal is obviously the same.

    Items with missing target or acquirer are treated as duplicates so
    coerce_deal_from_news doesn't push half-formed records through."""
    target = (item.get("target") or "").strip()
    acq = (item.get("acquirer") or "").strip()
    if not target or not acq:
        return True

    nt, na = _normalize_company(target), _normalize_company(acq)
    if not nt or not na:
        return True

    item_date = item.get("date")
    for e in existing:
        et = _normalize_company(e.get("target"))
        ea = _normalize_company(e.get("acquirer"))
        if not et or not ea:
            continue

        if not _strings_match(na, ea, threshold=0.7):
            continue

        if _strings_match(nt, et, threshold=0.8):
            return True

        if (_strings_match(nt, et, threshold=0.5)
                and _dates_within(item_date, e.get("date"), 60)):
            return True

        # Location proximity is a third axis but ONLY in combination with
        # at least weak target similarity. Without that floor, two
        # unrelated tuck-in acquisitions by the same acquirer in the same
        # state get false-positive-merged (saw it happen on first pass
        # with East Coast Resources vs Greenway Environmental, both
        # bought by Wind River within 60 days).
        if (_strings_match(nt, et, threshold=0.3)
                and _locations_within(item, e, 50)
                and _dates_within(item_date, e.get("date"), 60)):
            return True

    return False


# ----------------------------- web search -----------------------------
#
# We deliberately structure queries by category so the daily news feed
# isn't a deal-only tracker. Regulation, renewable-fuels policy, public-
# company earnings, restaurant industry signals, technology, labor cost,
# infrastructure, and ESG are all upstream drivers of FOG-industry
# economics. SEARCH_QUERIES is the canonical reference; we flatten it
# into EXPANDED_SEARCH_QUERIES for the existing search_for_updates entry
# points.

NEWS_CATEGORIES = [
    "M&A", "Regulatory", "Renewable Fuels", "Public Co.", "Restaurant",
    "Technology", "Labor/Ops", "Infrastructure", "Industry Events", "ESG",
]

# Backward-compat: the old prompt returned categories from a 4-value set.
# Map any legacy values into the new 10-category vocabulary at ingestion.
_LEGACY_CATEGORY_MAP = {
    "M&A": "M&A",
    "Regulation": "Regulatory",
    "Market": "Renewable Fuels",
    "Company News": "Public Co.",
}


SEARCH_QUERIES: dict[str, list[str]] = {
    "M&A": [
        '"grease trap" acquisition OR acquired OR merger',
        '"liquid waste" acquisition OR acquired OR merger',
        '"septic" "private equity" acquisition',
        '"Wind River Environmental" acquisition OR expansion',
        '"Liquid Environmental Solutions" OR "LES" acquisition',
        '"Waste Resources Management" OR "WRM" acquisition OR expansion',
        '"Southwaste" OR "SouthWaste Disposal" acquisition OR expansion',
        '"Silver City Processing" acquisition OR expansion',
        '"McDonald Farms" acquisition OR expansion',
        '"Neste" acquisition OR "has acquired" OR partnership UCO',
        '"SeQuential" OR "Crimson Renewable" acquisition OR expansion',
        '"used cooking oil" acquired OR acquisition OR merger',
        '"grease recycling" acquired OR acquisition',
        '"environmental services" "add-on" OR "platform" acquisition',
        '"has acquired" grease OR septic OR "liquid waste" OR UCO',
        'site:pehub.com grease OR septic OR "liquid waste" OR wastewater',
        'site:wastetodaymagazine.com acquisition',
    ],
    "Regulatory": [
        'FOG ordinance OR regulation OR enforcement grease trap',
        'EPA pretreatment program FOG update',
        'grease trap violation OR fine OR penalty restaurant',
        'sewer overflow grease OR FOG blockage',
        '"grease trap" compliance requirement new',
        'pretreatment program enforcement action grease',
    ],
    "Renewable Fuels": [
        'renewable diesel UCO "used cooking oil" feedstock',
        'sustainable aviation fuel SAF "cooking oil"',
        '"Neste" OR "SeQuential" UCO collection OR feedstock',
        '"Diamond Green Diesel" OR "Darling Ingredients" renewable',
        'renewable fuel standard RFS update',
        'LCFS "low carbon fuel" cooking oil grease',
        'yellow grease price OR market OR commodity',
        'UCO theft cooking oil stolen',
        'brown grease renewable fuel',
        'biodiesel grease feedstock market',
    ],
    "Public Co.": [
        '"Darling Ingredients" earnings OR revenue OR guidance',
        '"GFL Environmental" earnings OR results',
        '"Clean Harbors" earnings OR results',
        '"Barrel Energy" OR "BRLL" filing OR update',
        '"Darling Ingredients" analyst OR upgrade OR downgrade',
    ],
    "Restaurant": [
        'restaurant openings closings trends United States',
        '"National Restaurant Association" report OR data',
        'ghost kitchen growth trends',
        'restaurant industry employment trends',
        'food service industry outlook',
    ],
    "Technology": [
        '"grease trap" technology OR sensor OR monitoring OR IoT',
        'vacuum truck electric OR alternative fuel',
        'route optimization waste collection software',
        'waste-to-energy FOG OR grease',
        'anaerobic digestion FOG grease biogas',
        '"grease trap" innovation OR "new product"',
    ],
    "Labor/Ops": [
        'CDL driver shortage waste OR environmental services',
        'vacuum truck price OR cost',
        'waste hauler insurance rates',
        'DOT regulation waste hauler OR tanker',
    ],
    "Infrastructure": [
        'wastewater treatment plant upgrade OR expansion capacity',
        'POTW tipping fee change OR increase',
        'infrastructure funding water sewer IIJA',
        'combined sewer overflow FOG consent decree',
    ],
    "Industry Events": [
        'WWETT Show news OR announcement',
        'site:pumper.com grease OR FOG OR septic',
        'site:waste360.com liquid waste OR grease OR FOG OR septic',
        'site:wastetodaymagazine.com grease OR FOG OR septic',
        '"Water Environment Federation" FOG OR grease OR pretreatment',
    ],
    "ESG": [
        'circular economy grease OR FOG OR cooking oil',
        'sustainability reporting food service waste',
        'carbon credit grease OR cooking oil OR FOG',
    ],
}


def _flatten_queries() -> list[str]:
    """Flat list of queries across all categories. Used by the existing
    search_for_updates() default and the catchup script."""
    out: list[str] = []
    for cat, qs in SEARCH_QUERIES.items():
        out.extend(qs)
    return out


EXPANDED_SEARCH_QUERIES = _flatten_queries()

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
    """Legacy single-call prompt covering all 10 categories. Kept for the
    catchup script which uses one big sweep. The daily-refresh path now
    uses _build_category_prompt for per-category focused calls — see
    Bug 1 fix in search_for_updates."""
    # Compact seeds — one example per category. The full list lives in
    # SEARCH_QUERIES for reference; pasting them all in led to runaway
    # tool-call loops that exhausted the context window.
    seed_block = []
    for cat in NEWS_CATEGORIES:
        sample = [q.format(year=year) for q in SEARCH_QUERIES.get(cat, [])][:1]
        if sample:
            seed_block.append(f"  {cat}: {sample[0]}")
    seeds = "\n".join(seed_block)

    return f"""Today is {today}. You are populating an industry-briefing news feed for the non-hazardous liquid waste, grease trap (FOG), used cooking oil (UCO), septic services, and related environmental services industry in the United States.

Cover ALL of these 10 categories (do not focus only on M&A):

1.  M&A — acquisitions, mergers, divestitures, platform investments.
2.  Regulatory — new FOG ordinances at city/county level, EPA pretreatment program updates, state environmental agency enforcement (TCEQ, IDEM, MPCA, CDPHE, etc.), fines/penalties, sewer overflow events caused by FOG blockages.
3.  Renewable Fuels — renewable diesel, sustainable aviation fuel (SAF) involving UCO feedstock, RFS / LCFS policy, RIN pricing, Diamond Green Diesel news, UCO/yellow/brown grease commodity prices, UCO theft/fraud.
4.  Public Co. — Darling Ingredients (NYSE: DAR), GFL Environmental (NYSE: GFL), Clean Harbors (NYSE: CLH), Republic Services / Waste Connections / Casella, Barrel Energy (OTC: BRLL): earnings, segment results, analyst notes.
5.  Restaurant — opening/closing trends by metro, ghost kitchens, NRA reports, major-chain expansion/contraction (these drive customer-base size for haulers).
6.  Technology — smart grease-trap monitoring (IoT sensors), grease-trap design innovations, route optimization software, electric / alternative-fuel vacuum trucks, waste-to-energy / anaerobic digestion of FOG.
7.  Labor/Ops — CDL driver shortage and wages, vacuum truck pricing, fleet maintenance trends, OSHA/DOT changes, environmental-services insurance market.
8.  Infrastructure — POTW upgrades and capacity, tipping-fee schedules, IIJA/BIL water/sewer funding, CSO consent decrees, municipal pretreatment audits.
9.  Industry Events — WWETT Show, Pumper.com, Waste360, Waste Today features, Water Environment Federation, National Pretreatment Conference, Environmental Business Journal reports.
10. ESG — circular economy involving FOG waste, sustainability reporting requirements, carbon credit markets for FOG, Scope 3 tracking by food service.

CRITICAL: do NO MORE than 6 web_search calls total. Pick the highest-value searches across categories — breadth matters more than depth. Each web_search response is large; we need to stay well under 200k cumulative context.

Seed searches (one example per category, for inspiration only — synthesize your own queries as needed):
{seeds}

{window_note}

For each result return a JSON object:
- date (YYYY-MM-DD) — the PUBLICATION DATE of the article, taken from the article body. NEVER a date in the future relative to {today}. If only a month is given, use the first of the month. If you cannot determine the date from the article body, OMIT the result rather than guessing. Watch out for date format ambiguity: "3/15" is March 15, not May 15.
- headline
- source (publication name)
- source_url — the EXACT article URL deep-linked to the specific article. NEVER a homepage like "https://example.com" or a generic "/news" listing. If you only have a homepage, OMIT the result.
- category — exactly one of: {", ".join('"' + c + '"' for c in NEWS_CATEGORIES)}
- summary (2-3 sentences)
- relevance_score — integer 1 to 5 using these strict criteria:
    5 = Directly about the FOG / grease-trap / liquid-waste / UCO / septic / rendering industry AND names a specific company in the space (LES, Wind River, Darling, Mahoney, Eazy Grease, WRM, Southwaste, Silver City Processing, McDonald Farms, etc.) or a specific deal in it.
    4 = Directly impacts FOG economics — e.g., renewable diesel policy affecting UCO prices, a specific municipal FOG ordinance, Darling/DAR PRO earnings with FOG-segment data, IoT grease-trap technology.
    3 = Closely adjacent — e.g., environmental services M&A broadly, waste-industry regulation that includes liquid waste, restaurant industry data with FOG implications.
    2 = Tangentially relevant — e.g., general trucking/CDL driver shortage, broad EPA policy, general PE deal activity in services, broad solid-waste M&A.
    1 = Loosely related — e.g., general economic news, broad sustainability trends.
  Only mark 4 or 5 if the article SPECIFICALLY discusses grease, FOG, liquid waste, UCO, septic, rendering, or names a company in this space. A general article about truck-driver shortages or broad waste-industry trends is a 2, NEVER a 4.
- is_deal — true ONLY for M&A transactions
- buyer, target, sponsor, location — for M&A only
- deal_size, multiple, deal_summary, owner_classification — for M&A only (see prior convention)
- date_confidence — "verified" if from article body, "approximate" if inferred

Target volume: 15-30 distinct items per refresh, spread across all 10 categories. Skip press releases that are thinly disguised advertisements. Prioritize specificity (a specific city's new FOG ordinance is more valuable than a "sustainability trends" overview). Return ONLY a JSON array; no surrounding prose."""


def _build_category_prompt(today: str, year: int, category: str,
                            cat_queries: list[str], window_note: str) -> str:
    """Focused prompt for a single news category. Each call asks for a
    bounded number of results so a 10-category daily refresh fits in
    max_tokens=8192 per call without truncation."""
    seeds = "\n".join(f"  - {q.format(year=year)}" for q in cat_queries[:5])
    return f"""Today is {today}. Search the web for recent {category} news in the non-hazardous liquid waste / grease trap / FOG / used cooking oil (UCO) / septic / rendering industry in the United States.

Seed queries (use 2-4 of these as web_search calls — be selective):
{seeds}

{window_note}

Return MAX 10-15 distinct results as a JSON array. Each object MUST include:
- date (YYYY-MM-DD) — the PUBLICATION DATE from the article body. NEVER a date in the future relative to {today}. If only a month is given, use the first of the month. If you cannot determine the date from the article body, OMIT the result.
- headline
- source (publication name)
- source_url — exact deep-link to the article. NEVER a homepage like "https://example.com" or a generic "/news" listing. If you only have a homepage, OMIT the result.
- category — for this run, set to "{category}".
- summary (2-3 sentences)
- relevance_score — integer 1-5. Use ONLY 4 or 5 if the item SPECIFICALLY discusses grease, FOG, liquid waste, UCO, septic, rendering, or names a company in this space (LES, Wind River, Darling, WRM, etc.). General trucking/CDL or broad solid-waste = 2.
- is_deal — true ONLY for M&A transactions
- buyer, target, sponsor, location — for M&A only
- deal_size, multiple, deal_summary, owner_classification — for M&A only
- date_confidence — "verified" if from article body, "approximate" if inferred

Skip press releases that are thinly disguised advertisements. Return ONLY a JSON array — no surrounding prose. Use no more than 4 web_search calls; we need to stay well under the response token budget."""


def _call_search_api(client, prompt: str, model: str, max_tokens: int):
    """Make one Claude web_search call and return parsed JSON list (or [])."""
    try:
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        sys.stderr.write(f"  API error: {e}\n")
        return []

    text_parts = [b.text for b in response.content if hasattr(b, "text")]
    full_text = "\n".join(text_parts)

    # Pick the largest balanced JSON array we can find.
    candidates: list[str] = []
    for m in re.finditer(r"\[", full_text):
        depth = 0
        for j, ch in enumerate(full_text[m.start():], start=m.start()):
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    candidates.append(full_text[m.start(): j + 1])
                    break
    candidates.sort(key=len, reverse=True)
    for c in candidates:
        try:
            parsed = json.loads(c)
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            continue

    sys.stderr.write(
        f"  no parseable JSON array. stop_reason="
        f"{getattr(response, 'stop_reason', '?')}, text len={len(full_text)}\n"
    )
    return []


def search_for_updates(queries: list[str] | None = None,
                        window_note: str | None = None) -> list[dict]:
    """Ask Claude (with web_search tool) for industry news.

    Daily-refresh path (queries=None): split into PER-CATEGORY API
    calls — one focused call per news category — so each response fits
    well within max_tokens and the model can give each category proper
    attention. Bug-1 fix: previously a single call covering all 10
    categories was hitting stop_reason=max_tokens and dropping every
    result.

    Catchup path (queries=non-None): single call covering the explicit
    catchup query list. Used by scripts/catchup.py.
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
    note = window_note or (
        f"Focus on items published in the LAST 7 DAYS. IGNORE any article "
        f"older than 14 days — we run this digest daily and don't want "
        f"landmark headlines from months ago re-surfaced. Today is {datetime.now().strftime('%Y-%m-%d')}; "
        f"only return articles dated within 14 days of today."
    )
    model = "claude-haiku-4-5-20251001"

    # Catchup path — single big call as before.
    if queries is not None:
        prompt = _build_search_prompt(today, year, queries, note)
        return _call_search_api(client, prompt, model, max_tokens=8192)

    # Daily-refresh path — one API call per category.
    import time
    all_results: list[dict] = []
    cats = list(SEARCH_QUERIES.keys())
    print(f"Querying {len(cats)} categories sequentially...")
    for i, cat in enumerate(cats):
        cat_queries = SEARCH_QUERIES.get(cat, [])
        if not cat_queries:
            continue
        prompt = _build_category_prompt(today, year, cat, cat_queries, note)
        print(f"  [{i+1}/{len(cats)}] {cat}...")
        items = _call_search_api(client, prompt, model, max_tokens=8192)
        # Stamp the canonical category onto every item — the model is
        # asked to set this but defending against drift is cheap.
        for it in items:
            it.setdefault("category", cat)
        print(f"    returned {len(items)} items")
        all_results.extend(items)
        # Pace requests to stay under the 30k input-tokens/minute rate
        # limit on this account tier (max_tokens counts against ITPM).
        if i + 1 < len(cats):
            time.sleep(8)

    print(f"Total raw results across all categories: {len(all_results)}")
    return all_results


# ----------------------------- merging --------------------------------


def normalize_category(raw_cat: str | None) -> str:
    """Map raw category string to one of the 10 canonical categories.
    Falls back to 'Industry Events' for unrecognized values rather than
    a default M&A label that would misclassify."""
    if not raw_cat:
        return "Industry Events"
    if raw_cat in NEWS_CATEGORIES:
        return raw_cat
    if raw_cat in _LEGACY_CATEGORY_MAP:
        return _LEGACY_CATEGORY_MAP[raw_cat]
    return "Industry Events"


# ============================================================================
# Multi-category classifier. The model assigns a single primary category to
# each news item, but most stories actually belong in 2-3 buckets — e.g.
# "WRM acquires Darling's trap grease business" is M&A + Public Co. + Renewable
# Fuels. Keyword rules below scan the headline + summary text and emit the
# set of categories that apply; the primary `category` field is kept for
# backward compatibility, and the array `categories` carries the full set
# with the primary listed first.
# ============================================================================

# Each entry: (canonical category name, compiled-regex pattern). Patterns
# are intentionally inclusive — false-positive cost is low (an article
# surfaces under an extra filter); false-negative cost is the original bug
# (article missing from a filter where it belongs).
_CATEGORY_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # M&A — deliberate verb forms; avoid generic words like "deal".
    ("M&A", re.compile(
        r"\bacqui[srz](?:e|es|ed|ing|ition)?\b|"
        r"\bmerger?(?:s|ed|ing)?\b|"
        r"\bmerges?\b|\bmerging\b|"
        r"\bdivest(?:s|ed|iture|ing)?\b|"
        r"\bcarve[\s-]?out\b|"
        r"\bplatform\s+investment\b|"
        r"\btuck[\s-]in\b|"
        r"\broll[\s-]up\b|"
        r"\bportfolio\s+(?:add[\s-]on|investment)\b|"
        r"\bcompletes?\s+acquisition\b|"
        r"\bpurchase(?:s|d)?\s+(?:assets|operations|business)\b",
        re.IGNORECASE)),

    # Public Co. — explicit company names plus ticker-mention pattern.
    # Avoids ambiguous 2-letter strings (WM alone would over-match).
    ("Public Co.", re.compile(
        r"\bDarling\s+Ingredients\b|\bDarling/?DAR\b|\bDAR\s+PRO\b|"
        r"\bDiamond\s+Green\s+Diesel\b|\bDGD\b|"
        r"\bGFL\b|\bGFL\s+Environmental\b|"
        r"\bClean\s+Harbors\b|\bHCCI\b|\bHeritage[\s-]?Crystal\s*Clean\b|"
        r"\bRepublic\s+Services\b|"
        r"\bWaste\s+Connections\b|"
        r"\bWaste\s+Management\b|\b\(NYSE:\s*WM\)|"
        r"\bCasella\b|"
        r"\bStericycle\b|"
        r"\bBarrel\s+Energy\b|\bBRLL\b|"
        r"\bNeste\b|"
        r"\bGoldman\s+Sachs\b|"
        r"\bVeolia\b|"
        r"\b(?:NYSE|NASDAQ|OTC|TSX|LSE):\s*[A-Z]+\b",
        re.IGNORECASE)),

    # Renewable Fuels — UCO / SAF / RD / RFS / RIN / yellow & brown grease.
    ("Renewable Fuels", re.compile(
        r"\bUCO\b|\bused\s+cooking\s+oil\b|"
        r"\brenewable\s+diesel\b|"
        r"\bSAF\b|\bsustainable\s+aviation\s+fuel\b|"
        r"\bRFS\b|\brenewable\s+fuel\s+standard\b|"
        r"\bRINs?\b(?!\s*g)|"  # avoid matching "rinse", "Rings"
        r"\bbiodiesel\b|\bbiofuel\b|"
        r"\byellow\s+grease\b|\bbrown\s+grease\b|"
        r"\btrap\s+grease\b|"  # 'trap grease' (substance) — feedstock for biodiesel
        r"\btallow\b|\brendering\b|"
        r"\bLCFS\b|\blow[\s-]carbon\s+fuel\b|"
        r"\bfeedstock\b.*\b(diesel|aviation|fuel|biodiesel|SAF)\b",
        re.IGNORECASE)),

    # Regulatory — agencies, ordinances, permits, decrees.
    ("Regulatory", re.compile(
        r"\bFOG\s+(?:ordinance|regulation|program|enforcement|control)\b|"
        r"\b(?:U\.?S\.?\s+)?EPA\b|"
        r"\bpretreatment\b|"
        r"\bsewer\s+(?:ordinance|regulation|use)\b|"
        r"\bclean\s+water\s+act\b|\bCWA\b|"
        r"\bconsent\s+decree\b|"
        r"\bdischarge\s+permit\b|"
        r"\bcompliance\s+(?:deadline|requirement|date)\b|"
        r"\bordinance\b.*\b(grease|FOG|wastewater)\b|"
        r"\b(?:pretreatment|industrial)\s+standards?\b",
        re.IGNORECASE)),

    # Technology — IoT, sensors, route opt, biogas, EV, hydrogen.
    ("Technology", re.compile(
        r"\bIoT\b|"
        r"\bsmart\s+(?:grease\s+trap|trap|sensor|monitor)\b|"
        r"\bgrease\s+trap\s+(?:sensor|monitoring|IoT)\b|"
        r"\bsensor\s+network\b|"
        r"\bfleet\s+(?:tech|optimi[sz]ation|management\s+software)\b|"
        r"\broute\s+optimi[sz]ation\b|"
        r"\bbiogas\b|\banaerobic\s+digest|"
        r"\b(?:electric|EV)\s+(?:truck|vehicle|fleet|garbage)\b|"
        r"\bhydrogen\s+fuel\s+cell\b|"
        r"\bAI[\s-]powered\b|"
        r"\bautomation\b.*\b(waste|collection|truck)\b",
        re.IGNORECASE)),

    # Labor/Ops — CDL, OSHA, insurance, fleet costs.
    ("Labor/Ops", re.compile(
        r"\bCDL\b|\bcommercial\s+driver(?:'s)?\s+license\b|"
        r"\bdriver\s+shortage\b|\bdriver(?:'s)?\s+pay\b|"
        r"\bOSHA\b|"
        r"\bFMCSA\b|"
        r"\bDOT\s+(?:regulation|rule|enforcement)\b|"
        r"\binsurance\s+(?:rate|premium|cost)\b|"
        r"\bfleet\s+(?:cost|expense|insurance)\b|"
        r"\bworkforce\b.*\b(waste|hauler|trucking|environmental)\b|"
        r"\bwage\b.*\b(driver|hauler|operator)\b",
        re.IGNORECASE)),

    # Restaurant — industry trends, NRA, ghost kitchens.
    ("Restaurant", re.compile(
        r"\brestaurant\s+(?:industry|sector|chains?|operators?)\b|"
        r"\bNational\s+Restaurant\s+Association\b|\bNRA\b|"
        r"\bghost\s+kitchen\b|"
        r"\bfood[\s-]?service\s+(?:industry|operator|sector)\b|"
        r"\bquick[\s-]service\s+restaurant\b|\bQSR\b|"
        r"\bdine[\s-]?in\b.*\btraffic\b|"
        r"\bfull[\s-]service\s+restaurant\b",
        re.IGNORECASE)),

    # Infrastructure — WWTP, POTW, IIJA, sewer overflows.
    ("Infrastructure", re.compile(
        r"\bWWTP\b|\bwastewater\s+treatment\s+(?:plant|facility)\b|"
        r"\bPOTW\b|"
        r"\bIIJA\b|\bBipartisan\s+Infrastructure\s+Law\b|"
        r"\bInfrastructure\s+Investment\s+and\s+Jobs\s+Act\b|"
        r"\bsewer\s+overflow\b|\bSSO\b|\bcombined\s+sewer\b|"
        r"\bwater\s+infrastructure\s+(?:fund|grant|funding)\b|"
        r"\bsewer\s+(?:upgrade|expansion|capacity)\b|"
        r"\bwater\s+reclamation\b|"
        r"\btreatment\s+plant\s+(?:upgrade|expansion)\b",
        re.IGNORECASE)),

    # Industry Events — conferences, shows, association meetings.
    ("Industry Events", re.compile(
        r"\bWWETT\b|\bWWETT\s+Show\b|"
        r"\bPumper\s+Show\b|\bpumper\.com\b|"
        r"\bWEF\b|\bWater\s+Environment\s+Federation\b|"
        r"\bWEFTEC\b|"
        r"\bResiduals\s+and\s+Biosolids\b|"
        r"\b(?:industry|trade)\s+(?:conference|show|expo|summit)\b",
        re.IGNORECASE)),

    # ESG — circular economy, sustainability reports, carbon credits.
    ("ESG", re.compile(
        r"\bcircular\s+economy\b|"
        r"\bsustainability\s+(?:report|reporting|disclosure)\b|"
        r"\bcarbon\s+credit\b|\bcarbon\s+offset\b|"
        r"\bScope\s+[123]\s+emissions?\b|"
        r"\bnet[\s-]zero\b|"
        r"\bGHG\s+emissions?\b|\bgreenhouse\s+gas\s+emissions?\b|"
        r"\bESG\s+(?:report|disclosure|metric|score)\b|"
        r"\bclimate\s+disclosure\b",
        re.IGNORECASE)),
]


def classify_categories(text: str | None, primary: str | None = None) -> list[str]:
    """Return all categories whose keyword pattern matches `text`. The
    primary (if given and valid) is moved to the front of the list so
    `categories[0]` is always a sensible default badge. Order otherwise
    follows the order of _CATEGORY_PATTERNS, which mirrors NEWS_CATEGORIES
    (M&A first → ESG last)."""
    text = text or ""
    matched: list[str] = []
    for name, pat in _CATEGORY_PATTERNS:
        if pat.search(text):
            matched.append(name)

    out: list[str] = []
    if primary and primary in NEWS_CATEGORIES:
        out.append(primary)
    for c in matched:
        if c not in out:
            out.append(c)
    # If nothing matched and no valid primary, fall back to a single
    # 'Industry Events' tag so downstream code never gets an empty list.
    if not out:
        out = ["Industry Events"]
    return out


def coerce_news_item(raw: dict) -> dict | None:
    """Normalize a search result into the news_feed.json schema. Rejects
    items with malformed dates or homepage-like source URLs. Tags items
    with future-dated 'publication date' as [Date unverified]."""
    if not raw.get("headline") or not raw.get("date"):
        return None
    if not valid_iso_date(raw["date"]):
        sys.stderr.write(f"reject (bad date): {raw.get('headline','')[:80]} — date={raw['date']!r}\n")
        return None
    if is_too_old(raw["date"]):
        sys.stderr.write(f"reject (too old, >{MAX_AGE_DAYS}d): {raw.get('headline','')[:80]} — date={raw['date']}\n")
        return None
    headline = raw["headline"]
    item_date = raw["date"]
    # Future-date guard: model occasionally returns tomorrow's date or a
    # search-index date that's after today. Don't reject — keep the item
    # but null the date and tag the headline so the analyst sees it.
    if is_future_date(item_date):
        sys.stderr.write(f"future date flagged: {headline[:80]} — date={item_date!r}\n")
        item_date = None  # null out
        if not headline.startswith("[Date unverified]"):
            headline = "[Date unverified] " + headline
    src_url = (raw.get("source_url") or "").strip()
    if src_url and looks_like_homepage(src_url):
        sys.stderr.write(f"reject (homepage URL): {raw.get('headline','')[:80]} — url={src_url}\n")
        return None
    cat = normalize_category(raw.get("category"))
    try:
        relevance = int(raw.get("relevance_score") or 3)
    except (TypeError, ValueError):
        relevance = 3
    relevance = max(1, min(5, relevance))
    clean_headline = clean_summary(headline) or headline
    clean_summary_text = clean_summary(raw.get("summary", "")) or ""
    categories = classify_categories(
        clean_headline + " " + clean_summary_text, primary=cat,
    )
    # Primary remains the model's pick (when it's a valid category) so the
    # category-color choice stays predictable; secondary chips come from
    # the keyword classifier.
    item: dict = {
        "date": item_date,
        "headline": clean_headline,
        "source": raw.get("source", ""),
        "source_url": src_url,
        "category": cat,
        "categories": categories,
        "summary": clean_summary_text,
        "relevance_score": relevance,
        "is_deal": bool(raw.get("is_deal")),
        "is_target_market": False,
        "target_market_name": None,
        "latitude": None,
        "longitude": None,
        "zoom_hint": None,
    }
    near = nearest_target_market(raw.get("location"))
    if near and near[1] <= TARGET_RADIUS_MI:
        item["is_target_market"] = True
        item["target_market_name"] = f"{near[0]} ({near[1]:.0f} mi)"
    geo = geocode_deal_location(raw.get("location"))
    if geo:
        item["latitude"], item["longitude"], item["zoom_hint"] = geo
    return item


def _normalize_summary_bullets(s) -> list[str] | None:
    if s is None:
        return None
    if isinstance(s, list):
        out = [clean_summary(str(x)) or "" for x in s]
        out = [b for b in out if b]
        return out or None
    text = clean_summary(str(s)) or ""
    text = text.strip()
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
    if not valid_iso_date(raw.get("date", "")):
        sys.stderr.write(f"reject deal (bad date): {raw.get('target','')[:60]} — date={raw.get('date')!r}\n")
        return None
    if is_too_old(raw["date"]):
        sys.stderr.write(f"reject deal (too old, >{MAX_AGE_DAYS}d): {raw.get('target','')[:60]} — date={raw['date']}\n")
        return None
    src_url = (raw.get("source_url") or "").strip()
    if src_url and looks_like_homepage(src_url):
        sys.stderr.write(f"reject deal (homepage URL): {raw.get('target','')[:60]} — url={src_url}\n")
        return None
    deal: dict = {
        "date": raw["date"],
        "target": raw["target"],
        "acquirer": raw["buyer"],
        "sponsor": raw.get("sponsor", ""),
        "location": raw.get("location", ""),
        "deal_type": raw.get("deal_type", "Add-On"),
        "deal_size": raw.get("deal_size", "Undisclosed"),
        "multiple": raw.get("multiple") or "N/A",
        "services": raw.get("services", ""),
        "source": raw.get("source", ""),
        "source_url": src_url,
        "is_target_market": False,
        "target_market_name": None,
        "owner_classification": raw.get("owner_classification") or "Unknown",
        "deal_summary": _normalize_summary_bullets(raw.get("deal_summary")),
        "date_confidence": raw.get("date_confidence") or "verified",
        "notes": clean_summary(raw.get("summary", "")) or "",
        "latitude": None,
        "longitude": None,
        "zoom_hint": None,
    }
    near = nearest_target_market(raw.get("location"))
    if near and near[1] <= TARGET_RADIUS_MI:
        deal["is_target_market"] = True
        deal["target_market_name"] = f"{near[0]} ({near[1]:.0f} mi)"
    geo = geocode_deal_location(raw.get("location"))
    if geo:
        deal["latitude"], deal["longitude"], deal["zoom_hint"] = geo
    return deal


# ============================================================================
# Deal → news synchronization. Every deal entered into comp_database.json
# must have a matching M&A news article in news_feed.json. This invariant
# was implicit before (both coerce_* paths ran on the same raw result) but
# leaked when coerce_news_item rejected on a homepage URL, near-dup match,
# or recency cutoff — the deal still got captured but no news existed.
# ============================================================================


def _deal_has_news(deal: dict, all_news: list[dict]) -> bool:
    """Conservative matcher used by both the live sync invariant and the
    retroactive sync_news_for_deals.py backfill. A news item counts as
    covering a deal if:

      - The news date is within 30 days of the deal date, AND
      - Distinctive tokens from both target AND acquirer appear in the
        news headline (or summary).

    Stricter than refresh_data.is_duplicate_news on purpose — we don't
    want to skip creating a needed news entry because some unrelated
    earnings story happened to share a company name.
    """
    nt_d = _normalize_company(deal.get("target"))
    na_d = _normalize_company(deal.get("acquirer"))
    if not nt_d or not na_d:
        return True  # nothing useful we could synthesize either
    t_tokens = set(nt_d.split())
    a_tokens = set(na_d.split())
    if not t_tokens or not a_tokens:
        return True
    deal_date = deal.get("date")
    for n in all_news:
        if not _dates_within(deal_date, n.get("date"), 30):
            continue
        text = ((n.get("headline") or "") + " " + (n.get("summary") or ""))
        text_tokens = set(_normalize_company(text).split())
        if (t_tokens & text_tokens) and (a_tokens & text_tokens):
            return True
    return False


def _deal_action_verb(deal_type: str | None) -> str:
    """Map a comp_database deal_type into the verb used in the
    synthesized headline. Keeps the wording natural for the common
    transaction structures rather than forcing 'Acquires' everywhere."""
    if not deal_type:
        return "Acquires"
    t = deal_type.lower()
    if "merge" in t:
        return "Merges With"
    if "divest" in t or "carve" in t:
        return "Acquires"
    if "asset" in t:
        return "Acquires Assets Of"
    if "investment" in t or "majority" in t or "minority" in t:
        return "Invests In"
    return "Acquires"


def synthesize_news_from_deal(deal: dict, first_seen: str) -> dict:
    """Build a news_feed.json-shaped item from a comp_database entry.
    Inherits Tier 2 flag, lat/lng, and zoom_hint directly from the deal
    so the map's "Find on Map" link still works."""
    acquirer = (deal.get("acquirer") or "").strip()
    target = (deal.get("target") or "").strip()
    verb = _deal_action_verb(deal.get("deal_type"))
    headline = f"{acquirer} {verb} {target}".strip()

    # Summary: prefer the deal_summary bullet list joined into a single
    # paragraph; fall back to the notes string when no bullets exist.
    summary_src = deal.get("deal_summary")
    if isinstance(summary_src, list) and summary_src:
        summary = " ".join(str(x) for x in summary_src if x)
    elif isinstance(summary_src, str) and summary_src.strip():
        summary = summary_src.strip()
    else:
        summary = (deal.get("notes") or "").strip()
    if not summary:
        # Last-resort summary derived from the structured fields so the
        # email/news card has something to show.
        bits = []
        if deal.get("location"):
            bits.append(f"Location: {deal['location']}")
        if deal.get("deal_type"):
            bits.append(f"Type: {deal['deal_type']}")
        if deal.get("deal_size") and deal["deal_size"] not in ("Undisclosed", "N/A"):
            bits.append(f"Size: {deal['deal_size']}")
        if deal.get("sponsor"):
            bits.append(f"Sponsor: {deal['sponsor']}")
        summary = ". ".join(bits) or f"{acquirer} announced acquisition of {target}."

    final_headline = clean_summary(headline) or headline
    final_summary = clean_summary(summary) or summary
    # Always M&A as primary (synth comes from a deal), but run the
    # classifier so Public Co. / Renewable Fuels / etc. surface from the
    # acquirer/target text too — e.g. WRM acquiring Darling adds
    # Public Co. and Renewable Fuels chips automatically.
    categories = classify_categories(
        final_headline + " " + final_summary, primary="M&A",
    )
    return {
        "date": deal.get("date"),
        "headline": final_headline,
        "source": deal.get("source", ""),
        "source_url": deal.get("source_url", ""),
        "category": "M&A",
        "categories": categories,
        "summary": final_summary,
        "relevance_score": 5,
        "is_deal": True,
        "is_target_market": bool(deal.get("is_target_market")),
        "target_market_name": deal.get("target_market_name"),
        "latitude": deal.get("latitude"),
        "longitude": deal.get("longitude"),
        "zoom_hint": deal.get("zoom_hint"),
        "first_seen_date": first_seen,
        "_synthesized_from_deal": True,
    }


# ----------------------------- archiving ------------------------------


def split_news_by_age(news: list[dict]) -> tuple[list[dict], list[dict]]:
    """Return (kept, archived). Items older than ARCHIVE_AFTER_DAYS go to
    the archive list."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=ARCHIVE_AFTER_DAYS)
    kept, archived = [], []
    for n in news:
        try:
            dt = datetime.fromisoformat(n["date"]).replace(tzinfo=timezone.utc)
        except (KeyError, ValueError, TypeError):
            # TypeError covers the case where n["date"] is None — items
            # whose publication date was flagged future-dated and nulled
            # by coerce_news_item. Keep them (don't archive items whose
            # age we can't determine).
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

    # Load the archive so the dedupe check sees every article we've
    # ever recorded — without this, a story that aged into the archive
    # could be re-introduced as "new" after MAX_AGE_DAYS expires from
    # the active feed.
    existing_archive: list[dict] = []
    if os.path.exists(ARCHIVE_PATH):
        with open(ARCHIVE_PATH, encoding="utf-8") as f:
            existing_archive = json.load(f)

    raw_results = search_for_updates()
    print(f"Search returned {len(raw_results)} raw results")

    today_iso = datetime.now(timezone.utc).date().isoformat()
    added_news = 0
    added_deals = 0
    synthesized_news = 0  # subset of added_news, for the log
    new_news_items: list[dict] = []
    new_deal_items: list[dict] = []
    for r in raw_results:
        n = coerce_news_item(r)
        news_added_this_iter = False
        # Dedupe against feed + archive in one combined view.
        if n and not is_duplicate_news(n, news + existing_archive):
            n["first_seen_date"] = today_iso
            news.append(n)
            new_news_items.append(n)
            added_news += 1
            news_added_this_iter = True
        d = coerce_deal_from_news(r)
        if d and not is_duplicate_deal(d, comps):
            comps.append(d)
            new_deal_items.append(d)
            added_deals += 1
            # Sync invariant: every deal must have a matching M&A news
            # article. If coerce_news_item rejected this raw result (e.g.
            # homepage URL) or is_duplicate_news suppressed it, and no
            # other news article in the database already covers the deal,
            # synthesize one from the deal fields.
            if not news_added_this_iter and not _deal_has_news(d, news + existing_archive):
                synth = synthesize_news_from_deal(d, today_iso)
                news.append(synth)
                new_news_items.append(synth)
                added_news += 1
                synthesized_news += 1
                sys.stderr.write(
                    f"  synth news from deal: {synth['headline'][:80]}\n"
                )

    # sort newest-first. Coerce None dates to a sentinel — items can land
    # here with date=None when the model returned a result without a
    # parseable publication date, and TypeError on None vs str would
    # crash the whole refresh.
    news.sort(key=lambda x: x.get("date") or "1900-01-01", reverse=True)
    comps.sort(key=lambda x: x.get("date") or "1900-01-01", reverse=True)

    # archive old news. existing_archive was loaded earlier for the
    # cross-file dedup; reuse it here so we don't reread the file.
    news, archived = split_news_by_age(news)
    if archived:
        existing_keys = {normalize_headline(a.get("headline", "")) for a in existing_archive}
        for a in archived:
            if normalize_headline(a.get("headline", "")) not in existing_keys:
                existing_archive.append(a)
        existing_archive.sort(key=lambda x: x.get("date") or "1900-01-01", reverse=True)
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

    # Sort new items newest-first so the daily email reads in
    # chronological order. The API returns items grouped by category, not
    # by date, so without this the digest jumps around the calendar.
    new_news_items.sort(key=lambda x: x.get("date") or "1900-01-01", reverse=True)
    new_deal_items.sort(key=lambda x: x.get("date") or "1900-01-01", reverse=True)

    # Split first-seen items into "real new today" vs "backfill". A
    # backfill item is one we hadn't seen before but whose publication
    # date is older than 7 days — we keep it in the database but never
    # surface it in the email's "NEW TODAY" section. Caught the May 16
    # bug where a May 6 Clean Harbors article landed in NEW TODAY ten
    # days late.
    today = datetime.now(timezone.utc).date()
    cutoff_iso = (today - timedelta(days=7)).isoformat()
    new_today_news = [n for n in new_news_items
                       if n.get("date") and n["date"] >= cutoff_iso]
    backfill_news = [n for n in new_news_items
                      if not (n.get("date") and n["date"] >= cutoff_iso)]

    last_refresh = {
        "timestamp": metadata["lastRefreshed"],
        "added_news_count": added_news,
        "added_deals_count": added_deals,
        "archived_count": len(archived),
        "new_today_news": new_today_news,
        "backfill_news": backfill_news,
        "new_deals": new_deal_items,
        # Kept for backward compatibility — anything reading the old key
        # gets the full list as before.
        "new_news": new_news_items,
    }
    with open(os.path.join(ROOT, "data", "last_refresh.json"), "w", encoding="utf-8") as f:
        json.dump(last_refresh, f, indent=2)

    # Per-category breakdown of the news feed (post-merge state).
    cat_counts: dict[str, int] = {c: 0 for c in NEWS_CATEGORIES}
    for n in news:
        cat = normalize_category(n.get("category"))
        cat_counts[cat] = cat_counts.get(cat, 0) + 1
    breakdown = ", ".join(f"{c}={cat_counts[c]}" for c in NEWS_CATEGORIES if cat_counts[c])
    categories_present = sum(1 for c in cat_counts if cat_counts[c] > 0)

    synth_note = (
        f" (of which {synthesized_news} synth'd from deals)"
        if synthesized_news else ""
    )
    print(
        f"Refresh complete. {len(news)} total items across {categories_present} categories. "
        f"Breakdown: {breakdown or '(none)'}. "
        f"Added this run: {added_news} news{synth_note}, {added_deals} deals. "
        f"Archived: {len(archived)}. Comps total: {len(comps)}."
    )

    if added_news < 10 and len(raw_results) > 0:
        sys.stderr.write(
            f"WARNING: only {added_news} new items added (target: 15-30). "
            "Search queries may need further broadening, or daily news pipeline "
            "is producing duplicates against existing feed.\n"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
