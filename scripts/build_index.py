#!/usr/bin/env python3
"""Assemble docs/index.html from the existing FOG facility map plus the
news + transaction comp tabs.

The original map HTML lives at ../fog_map_project/fog_facility_map.html (or
the path given by the FOG_MAP_HTML env var). It contains ~5.8 MB of inline
FOG_DATA / WWTP_DATA. We stream it through Python so those arrays never
have to be loaded into another tool's working memory.

This script is run once at repo init, and again any time the map data is
regenerated. The daily refresh script (refresh_data.py) only updates the
news + comp data blocks via marker replacement; it does not invoke this.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_MAP_DEFAULT = os.path.expanduser("~/fog_map_project/fog_facility_map.html")
SRC_MAP = os.environ.get("FOG_MAP_HTML", SRC_MAP_DEFAULT)
DST = os.path.join(ROOT, "docs", "index.html")
NEWS_JSON = os.path.join(ROOT, "data", "news_feed.json")
COMPS_JSON = os.path.join(ROOT, "data", "comp_database.json")


def slice_original(path: str) -> tuple[str, str, str]:
    """Return (style_inner, body_inner, scripts_block).

    style_inner: contents between <style> and </style> in the original.
    body_inner:  HTML between <body> and the first <script ...>; the map
                 div and floating panels live here.
    scripts_block: from the first <script ...> through the last </script>;
                 includes the leaflet CDN tags and the inline FOG_DATA /
                 WWTP_DATA / map-init code.
    """
    with open(path, encoding="utf-8") as f:
        html = f.read()

    m = re.search(r"<style>(.*?)</style>", html, re.DOTALL)
    style_inner = m.group(1) if m else ""

    body_start = html.find("<body>")
    if body_start < 0:
        raise RuntimeError("could not find <body> in original map HTML")
    body_start += len("<body>")
    first_script = html.find("<script", body_start)
    body_inner = html[body_start:first_script]

    end_close = html.rfind("</script>")
    scripts = html[first_script : end_close + len("</script>")]
    return style_inner, body_inner, scripts


def inject_map_handle(scripts: str) -> str:
    """Expose the Leaflet map instance + marker collections globally so
    the tab-switcher and Find-on-Map handlers can drive the map."""
    last = scripts.rfind("</script>")
    handle = (
        "\nwindow.__fogMap = (typeof map !== 'undefined') ? map : null;\n"
        "window.__fogMarkers = (typeof fogMarkers !== 'undefined') ? fogMarkers : null;\n"
        "window.__fogClusters = (typeof fogClusters !== 'undefined') ? fogClusters : null;\n"
        "window.__fogCatOrder = (typeof CAT_ORDER !== 'undefined') ? CAT_ORDER : null;\n"
        "window.dispatchEvent(new Event('fogMapReady'));\n"
    )
    return scripts[:last] + handle + scripts[last:]


# ============================================================================
# Public-company reclassification (Change 2 in spec)
#
# The upstream map separates owners into Tier-1 consolidators (LES, WRE, BAK,
# DAR, MAH, ...), Tier-2 "Other PE-Backed", regional, local, and municipal.
# Several of those are actually publicly traded — they should not be tagged
# PE-Backed. We add a new PUB tier (navy #2C3E50) and reclassify the affected
# facilities at build time, leaving the upstream pipeline untouched.
# ============================================================================

# Simplified 9-bucket taxonomy (per the UI redesign). Per-ct reclassifications:
#   DAR — stays as DAR (Darling has its own orange bucket)
#   BAR (Barrel Energy)         → PUB  (Public Company tier, navy)
#   MAH (Mahoney/Crimson/Neste) → PE   (folded into PE-Backed Other)
#   MOM (Momentum)              → PE
#   SEP (Septic Blue)           → PE
#   EAZ (Eazy Grease)           → REG  (private regional)
_CT_TO_PUBLIC_LABEL = {
    "BAR": "Public: Barrel Energy (OTC: BRLL)",
}
_CT_TO_PE_LABEL = {
    "MAH": "PE-Backed: Mahoney / Crimson (Neste)",
    "MOM": "PE-Backed: Momentum Environmental",
    "SEP": "PE-Backed: Septic Blue (Georgia Oak)",
}
_CT_TO_REG_LABEL = {
    "EAZ": "Regional: Eazy Grease (Private)",
}

# For records currently tagged ct=PE (Tier-2), reclassify if the owner_type
# string contains one of these substrings (case-insensitive).
_OT_SUBSTRING_TO_PUBLIC_LABEL = [
    ("hepaco", "Public: Clean Harbors (NYSE: CLH)"),
    ("clean harbors", "Public: Clean Harbors (NYSE: CLH)"),
    ("republic services", "Public: Republic Services (NYSE: RSG)"),
    ("us ecology", "Public: Republic Services (NYSE: RSG)"),
    ("waste connections", "Public: Waste Connections (NYSE: WCN)"),
    ("gfl environmental", "Public: GFL Environmental (NYSE: GFL)"),
    ("waste management", "Public: WM (NYSE: WM)"),
    ("stericycle", "Public: Stericycle (NASDAQ: SRCL)"),
    ("casella", "Public: Casella Waste (NASDAQ: CWST)"),
]

# Last-resort fuzzy match on facility name + operator (catches Barrel /
# Happy Traps before they were added to the upstream brand list, and any
# Darling-owned location whose `ct` somehow stayed un-tagged).
_NAME_OP_PATTERNS_TO_PUBLIC_LABEL = [
    (re.compile(r"\bbarrel energy\b|\bhappy traps\b", re.IGNORECASE),
     "Public: Barrel Energy (OTC: BRLL)"),
    # Darling stays in its own DAR bucket (orange) per the simplified
    # 9-bucket taxonomy — do NOT reclassify Darling-named facilities to PUB.
]


# ============================================================================
# FOG-only filter
#
# The upstream FOG_DATA is built from EPA FRS facilities matching a broad
# set of NAICS codes — including 562219 ("Other Nonhazardous Waste
# Treatment & Disposal") which sweeps in landfills, transfer stations,
# recycling centers, and many other facilities that aren't FOG/grease/
# UCO/septic/liquid-waste. Tighten the dataset by:
#
#   1) Hard-removing names that contain landfill / transfer-station /
#      recycling / hazardous-waste / etc. keywords.
#   2) For ambiguous NAICS (562219 / 562111), keeping only facilities
#      whose name contains a FOG-positive term.
#   3) For facilities owned by primarily-solid-waste public companies
#      (Republic / WM / GFL / Casella / Stericycle / Clean Harbors / US
#      Ecology), keeping only those whose name contains a *core* FOG
#      term — even tighter than (2), because their default product is
#      not FOG.
#
# NAICS 562991 (Septic Tank and Related Services) and 311613 (Rendering
# and Meat Byproduct Processing) are kept regardless — they're
# inherently FOG-relevant.
# ============================================================================

# Municipal POTW name fragments. The map already has a separate WWTP layer
# (~14k facilities) for treatment plants — facilities matching these patterns
# in FOG_DATA are duplicates and not FOG-relevant for our purposes (they
# RECEIVE waste, they don't haul or process FOG/UCO).
_POTW_NAME_PATTERNS = [s.lower() for s in [
    "wwtp", "wwtf", "wpcp", "wpcf", "potw",
    # "wastewater treatment" with or without a plant/facility suffix —
    # municipalities often name their plant just "{TOWN} WASTEWATER TREATMENT".
    "wastewater treatment",
    "wastewater reclamation",
    "wastewater system", "wastewater utility",
    "water treatment plant", "water treatment facility",
    "water reclamation",
    "sewage treatment", "sewage lagoon", "sewer lagoon", "wastewater lagoon",
    "waste water treatment",  # space variant
    "treatment lagoon",
    "water authority", "water district", "water utility",
    "sewer district", "sewer authority", "sanitation district",
]]

_NEGATIVE_KEYWORDS = [s.lower() for s in [
    # Solid waste
    "landfill", "transfer station", "recycling center", "recycling facility",
    "materials recovery", "MRF", "solid waste", "refuse", "trash",
    "garbage", "rubbish", "municipal waste",
    # Construction / demolition
    "construction debris", "C&D", "demolition", "concrete recycling",
    "asphalt", "aggregate", "quarry", "mining",
    # Composting / organics (non-FOG)
    "composting", "compost facility", "yard waste", "green waste",
    "mulch", "wood waste", "biomass",
    # Scrap / salvage
    "scrap", "salvage", "auto parts", "junkyard", "wrecking",
    "tire", "rubber", "electronics", "e-waste",
    # Hazardous waste
    "hazardous waste", "RCRA", "PCB", "radioactive", "nuclear",
    "chemical waste", "toxic",
    # Medical / pharmaceutical
    "medical waste", "biohazard", "pharmaceutical", "sharps",
    "pathological", "infectious waste", "stericycle",
    # Other non-FOG
    "paper mill", "textile", "plastic recycling", "glass recycling",
    "metal recycling", "aluminum", "steel",
    "incinerator", "waste-to-energy",
    "storage tank", "fuel storage", "petroleum bulk",
    "gas station", "convenience store", "compressor station",
    "snow dump", "natural gas storage",
    "car wash", "laundry", "dry cleaner",
    "animal hospital", "veterinary",
    "cemetery", "funeral",
]]

_FOG_POSITIVE_KEYWORDS = [s.lower() for s in [
    # Core FOG
    "grease", "FOG", "fats oils", "fat oil",
    "cooking oil", "UCO", "used oil", "yellow grease", "brown grease",
    "trap cleaning", "trap service", "trap pumping",
    # Liquid waste
    "liquid waste", "liquid environmental", "liquid disposal",
    "non-hazardous liquid", "nonhazardous liquid",
    # Note: bare "wastewater" / "wastewater treatment" / "water treatment"
    # are NOT positive — those match POTWs (handled by _POTW_NAME_PATTERNS
    # which runs first). Keep only hauler / service variants.
    "wastewater service", "wastewater hauling", "wastewater hauler",
    # Septic / pumping
    "septic", "cesspool", "pump", "vacuum truck",
    "sewer", "drain", "rooter", "plumbing",
    "jetting", "hydro", "vac",
    # Rendering / processing
    "rendering", "tallow", "biodiesel", "biofuel",
    "protein", "meat byproduct", "animal fat",
    "recycling grease", "grease recycling",
    "oil recovery", "oil recycling", "oil collection",
    # Environmental services (generic but often FOG-related)
    "environmental service", "environmental solution",
    "environmental management",
    # Industry-specific
    "porta", "portable", "restroom",
    "catch basin", "storm drain",
    "industrial cleaning", "industrial service",
    # Known brand fragments
    "wind river", "LES", "liquid enviro", "dar pro", "darling",
    "baker commodit", "mahoney", "eazy grease", "happy traps",
    "barrel energy", "septic blue",
]]

_SOLID_WASTE_COMPANY_PATTERNS = [s.lower() for s in [
    "republic services", "allied waste",
    "waste management", " wm ", "waste connections",
    " gfl ", "casella", "waste industries",
    "advanced disposal", "stericycle",
    "covanta", "wheelabrator",
    "clean harbors",
    "us ecology",
    "safety-kleen", "safety kleen", "clean earth",
]]

_CORE_FOG_TERMS = [s.lower() for s in [
    "grease", "FOG", "cooking oil", "UCO", "liquid waste",
    "septic", "rendering", "tallow", "wastewater",
    "oil recovery", "oil recycling",
]]

_NAICS_KEEP_REGARDLESS = {"562991", "311613"}      # Septic + Rendering
_NAICS_AMBIGUOUS = {"562219", "562111", "221320"}  # Need FOG-positive name


def filter_to_fog_only(records: list[dict]) -> tuple[list[dict], dict[str, int]]:
    """Return (filtered_records, counts). counts breaks out how many
    records were dropped under each rule and how many survived under each
    keep rule, so the operator can see what the filter is doing."""
    counts: dict[str, int] = {
        "kept_naics_core": 0,
        "kept_ambig_naics_with_keyword": 0,
        "kept_keyword_only": 0,
        "removed_potw": 0,
        "removed_solid_waste_company": 0,
        "removed_negative_keyword": 0,
        "removed_no_fog_signal": 0,
    }
    out: list[dict] = []

    for r in records:
        name = (r.get("n") or "").lower()
        op = (r.get("op") or "").lower()
        ot = (r.get("ot") or "").lower()
        naics_str = (r.get("na") or "")
        naics = {p.strip() for p in re.split(r"[;,]", naics_str) if p.strip()}

        owner_text = name + " " + op + " " + ot
        is_potw = any(p in name for p in _POTW_NAME_PATTERNS)
        is_solid_waste_co = any(c in owner_text for c in _SOLID_WASTE_COMPANY_PATTERNS)
        has_core_fog = any(t in name for t in _CORE_FOG_TERMS)
        has_negative = any(kw in name for kw in _NEGATIVE_KEYWORDS)
        has_positive = any(kw in name for kw in _FOG_POSITIVE_KEYWORDS)

        # Step 0: POTW name pattern — these are municipal sewage treatment
        # plants already covered by the WWTP layer. Highest-priority cut.
        if is_potw:
            counts["removed_potw"] += 1
            continue

        # Step 4: solid-waste-company filter is the strictest cut; runs first.
        if is_solid_waste_co and not has_core_fog:
            counts["removed_solid_waste_company"] += 1
            continue

        # Step 2: hard-negative keyword unless rescued by FOG-positive term.
        if has_negative and not has_positive:
            counts["removed_negative_keyword"] += 1
            continue

        # Step 3: NAICS-based decisions.
        if naics & _NAICS_KEEP_REGARDLESS:
            out.append(r)
            counts["kept_naics_core"] += 1
            continue
        if naics & _NAICS_AMBIGUOUS:
            if has_positive:
                out.append(r)
                counts["kept_ambig_naics_with_keyword"] += 1
            else:
                counts["removed_no_fog_signal"] += 1
            continue

        # No relevant NAICS — must have a FOG-positive keyword to survive.
        if has_positive:
            out.append(r)
            counts["kept_keyword_only"] += 1
        else:
            counts["removed_no_fog_signal"] += 1

    return out, counts


# ============================================================================
# Eazy Grease — Florida-rooted UCO consolidator we missed in the upstream
# brand list. Until ownership.py adds them, patch matching facilities at
# build time. Classified as a Regional platform (REG bucket).
# ============================================================================
_EAZY_GREASE_PATTERN = re.compile(
    r"\beazy\s*grease\b|"
    r"\bdht\s*grease\b|"
    r"\brelentless\s*renewables\b|"
    r"\bdaytona\s*biodiesel\b|"
    r"\bcleanfri\b|"
    r"\bliquid\s*recovery\s*solutions\b|"
    r"\bgreen\s*nature\s*recycling\b",
    re.IGNORECASE,
)
_EAZY_GREASE_LABEL = "Regional: Eazy Grease (Private)"


# Names that look like a solid-waste public-company match but are actually
# independent local operators (the upstream brand match in ownership.py uses
# `\bwaste management\b` which catches anything ending in "Waste Management",
# producing false positives for businesses literally named "Liquid Waste
# Management" or "Septic Services Waste Management"). When these turn up,
# pull them out of the Public Co. tier and back into Local.
_FALSE_PUBLIC_NAME_FRAGMENTS = (
    "liquid waste management",
    "septic services waste management",
)


def reclassify_to_public(records: list[dict]) -> dict[str, int]:
    """Mutate records in place to fit the simplified 9-bucket taxonomy.
    Return a count breakdown by new label (used only for the build summary
    print)."""
    counts: dict[str, int] = {}

    def _bump(label: str) -> None:
        counts[label] = counts.get(label, 0) + 1

    for r in records:
        ct = r.get("ct", "")
        name_lower = (r.get("n") or "").lower()

        # Upstream brand-match false positives ("Liquid Waste Management Inc"
        # tagged as Waste Management) — demote to Local rather than rolling
        # them into Public Company.
        if any(frag in name_lower for frag in _FALSE_PUBLIC_NAME_FRAGMENTS):
            ot = r.get("ot", "")
            if ot.startswith("PE-Backed:"):
                r["ot"] = "Local: " + (r.get("n") or "").title()
                r["ct"] = "LOC"
            continue

        new_ct = None
        new_label = None

        # Direct ct → bucket reclassifications.
        if ct in _CT_TO_PUBLIC_LABEL:
            new_ct, new_label = "PUB", _CT_TO_PUBLIC_LABEL[ct]
        elif ct in _CT_TO_PE_LABEL:
            new_ct, new_label = "PE", _CT_TO_PE_LABEL[ct]
        elif ct in _CT_TO_REG_LABEL:
            new_ct, new_label = "REG", _CT_TO_REG_LABEL[ct]
        elif ct == "PE":
            # Existing Tier-2 PE facilities — promote to PUB if the brand
            # is actually a public company, else leave as PE.
            ot_lower = (r.get("ot") or "").lower()
            for sub, lbl in _OT_SUBSTRING_TO_PUBLIC_LABEL:
                if sub in ot_lower:
                    new_ct, new_label = "PUB", lbl
                    break

        if new_label is None:
            # Last-resort fuzzy match for things upstream missed (e.g.
            # Barrel/Happy Traps or Darling siblings without ct tag).
            text = ((r.get("n") or "") + " " + (r.get("op") or "")).lower()
            for rx, lbl in _NAME_OP_PATTERNS_TO_PUBLIC_LABEL:
                if rx.search(text):
                    new_ct, new_label = "PUB", lbl
                    break

        if new_label is not None:
            r["ot"] = new_label
            r["ct"] = new_ct
            _bump(new_label)

    return counts


def reclassify_eazy_grease(records: list[dict]) -> int:
    """Tag any facility whose name or operator matches an Eazy Grease brand
    as Regional: Eazy Grease (Private). Returns count reclassified."""
    n = 0
    for r in records:
        if r.get("ct") == "PUB":
            continue  # leave public-co reclass alone
        text = ((r.get("n") or "") + " " + (r.get("op") or "")).lower()
        if _EAZY_GREASE_PATTERN.search(text):
            r["ot"] = _EAZY_GREASE_LABEL
            r["ct"] = "REG"
            n += 1
    return n


_NEW_CATEGORY_INFO = """const CATEGORY_INFO = {
  "LES": {label:"LES (Goldman Sachs)",   color:"#e74c3c"},
  "WRE": {label:"Wind River (Gryphon)",  color:"#3498db"},
  "BAK": {label:"Baker Commodities",     color:"#27ae60"},
  "DAR": {label:"Darling / DAR PRO",     color:"#f39c12"},
  "PUB": {label:"Public Company",        color:"#2C3E50"},
  "PE":  {label:"PE-Backed (Other)",     color:"#c0392b"},
  "REG": {label:"Regional Operator",     color:"#1abc9c"},
  "LOC": {label:"Local / Family",        color:"#95a5a6"},
  "UNK": {label:"Unknown",               color:"#bdc3c7"},
};"""

_NEW_CAT_ORDER = (
    'const CAT_ORDER = ["LES","WRE","BAK","DAR","PUB","PE","REG","LOC","UNK"];'
)
_NEW_NEW_ENTRANT_CATS = 'const NEW_ENTRANT_CATS = new Set();'


def patch_category_info(scripts: str) -> str:
    """Replace CATEGORY_INFO / CAT_ORDER / NEW_ENTRANT_CATS in the original
    map JS with the version that includes the PUB tier and drops standalone
    DAR / BAR entries."""
    # Inner per-category objects end with `},` so we can safely non-greedy
    # match up to the first `};` which only occurs at the literal's end.
    scripts, n = re.subn(
        r"const CATEGORY_INFO = \{.*?\};",
        _NEW_CATEGORY_INFO,
        scripts, count=1, flags=re.DOTALL,
    )
    if n != 1:
        sys.stderr.write("WARNING: CATEGORY_INFO replacement did not match\n")
    scripts, n = re.subn(
        r"const CAT_ORDER = \[[^\]]+\];",
        _NEW_CAT_ORDER,
        scripts, count=1,
    )
    if n != 1:
        sys.stderr.write("WARNING: CAT_ORDER replacement did not match\n")
    scripts, n = re.subn(
        r"const NEW_ENTRANT_CATS = new Set\(\[[^\]]+\]\);",
        _NEW_NEW_ENTRANT_CATS,
        scripts, count=1,
    )
    if n != 1:
        sys.stderr.write("WARNING: NEW_ENTRANT_CATS replacement did not match\n")
    return scripts


_NEW_LEGEND_BODY = """
    <table style="font-size:11px;">
      <tr><td colspan="2" style="font-size:10px; color:#666; text-transform:uppercase; letter-spacing:0.4px; padding-bottom:2px;">Marker shape</td></tr>
      <tr><td><span style="display:inline-block; width:12px; height:12px; border-radius:50%; background:#444; vertical-align:middle;"></span></td><td>● Plant (processing)</td></tr>
      <tr><td><span style="display:inline-block; width:9px; height:9px; background:#444; transform:rotate(45deg); vertical-align:middle; margin-left:1px;"></span></td><td>◆ Operator (collection)</td></tr>
      <tr><td><span class="swatch-d"></span></td><td>◇ WWTP (municipal)</td></tr>
      <tr><td colspan="2" style="font-size:10px; color:#666; text-transform:uppercase; letter-spacing:0.4px; padding-top:6px; padding-bottom:2px;">Owner color</td></tr>
      <tr><td><span class="swatch" style="background:#e74c3c;"></span></td><td>LES (Goldman Sachs)</td></tr>
      <tr><td><span class="swatch" style="background:#3498db;"></span></td><td>Wind River (Gryphon)</td></tr>
      <tr><td><span class="swatch" style="background:#27ae60;"></span></td><td>Baker Commodities</td></tr>
      <tr><td><span class="swatch" style="background:#f39c12;"></span></td><td>Darling / DAR PRO</td></tr>
      <tr><td><span class="swatch" style="background:#2C3E50;"></span></td><td>Public Company</td></tr>
      <tr><td><span class="swatch" style="background:#c0392b;"></span></td><td>PE-Backed (Other)</td></tr>
      <tr><td><span class="swatch" style="background:#1abc9c;"></span></td><td>Regional Operator</td></tr>
      <tr><td><span class="swatch" style="background:#95a5a6;"></span></td><td>Local / Family</td></tr>
      <tr><td><span class="swatch" style="background:#bdc3c7;"></span></td><td>Unknown</td></tr>
    </table>
    <div class="muted" style="margin-top:6px; padding-top:6px; border-top:1px solid #eee;">
      Visible: <span id="vis-plants">—</span> plants, <span id="vis-ops">—</span> operators
    </div>
"""


def patch_legend(body_inner: str) -> str:
    """Replace the entire legend body with the simplified shape +
    color two-section layout."""
    body_inner = re.sub(
        r'(<div id="legend" class="panel">.*?<div class="panel-body">).*?(</div>\s*</div>)',
        lambda m: m.group(1) + _NEW_LEGEND_BODY + "  " + m.group(2),
        body_inner,
        count=1,
        flags=re.DOTALL,
    )
    return body_inner


# ============================================================================
# Service-HQ markers — companies that acquire in the FOG space but don't
# own processing plants. Plotted as diamond markers in a separate layer
# group, hidden by default behind a filter-panel checkbox per company.
# ============================================================================

SERVICE_HQ_DATA: list[dict] = [
    # Momentum Environmental
    {"company": "Momentum Environmental", "key": "momentum", "color": "#6C3483",
     "lat": 40.713, "lng": -74.006, "primary": True,
     "city": "New York, NY", "sponsor": "PE-backed (sponsor TBD)",
     "description": "Environmental services focus in NY metro.",
     "acq_count": "3+ acquisitions since 2024"},

    # Septic Blue (Georgia Oak Partners)
    {"company": "Septic Blue", "key": "septic-blue", "color": "#1E8449",
     "lat": 33.749, "lng": -84.388, "primary": True,
     "city": "Atlanta, GA", "sponsor": "Georgia Oak Partners",
     "description": "Residential septic focus.",
     "acq_count": "Platform acquisition (Feb 2024)"},
    {"company": "Septic Blue", "key": "septic-blue", "color": "#1E8449",
     "lat": 35.227, "lng": -80.843, "primary": False,
     "city": "Charlotte, NC", "sponsor": "Georgia Oak Partners",
     "description": "Residential septic focus.",
     "acq_count": "Platform acquisition (Feb 2024)"},
    {"company": "Septic Blue", "key": "septic-blue", "color": "#1E8449",
     "lat": 35.780, "lng": -78.638, "primary": False,
     "city": "Raleigh, NC", "sponsor": "Georgia Oak Partners",
     "description": "Residential septic focus.",
     "acq_count": "Platform acquisition (Feb 2024)"},

    # Eazy Grease (Private)
    {"company": "Eazy Grease", "key": "eazy-grease", "color": "#D4AC0D",
     "lat": 27.951, "lng": -82.459, "primary": True,
     "city": "Tampa Bay, FL", "sponsor": "Private (no PE sponsor identified)",
     "description": "UCO/grease recycling. Florida-focused, expanding multi-state.",
     "acq_count": "5 acquisitions + 1 merger"},
    {"company": "Eazy Grease", "key": "eazy-grease", "color": "#D4AC0D",
     "lat": 30.438, "lng": -84.281, "primary": False,
     "city": "Tallahassee, FL", "sponsor": "Private (no PE sponsor identified)",
     "description": "UCO/grease recycling. Florida-focused, expanding multi-state.",
     "acq_count": "5 acquisitions + 1 merger"},
    {"company": "Eazy Grease", "key": "eazy-grease", "color": "#D4AC0D",
     "lat": 28.538, "lng": -81.379, "primary": False,
     "city": "Central Florida", "sponsor": "Private (no PE sponsor identified)",
     "description": "UCO/grease recycling. Florida-focused, expanding multi-state.",
     "acq_count": "5 acquisitions + 1 merger"},
    {"company": "Eazy Grease", "key": "eazy-grease", "color": "#D4AC0D",
     "lat": 26.122, "lng": -80.137, "primary": False,
     "city": "South Florida", "sponsor": "Private (no PE sponsor identified)",
     "description": "UCO/grease recycling. Florida-focused, expanding multi-state.",
     "acq_count": "5 acquisitions + 1 merger"},
]

# Hidden no-op stubs for the per-company HQ toggles. The original
# inject_service_hq() JS still wires up event listeners on these IDs;
# leaving the inputs in (display:none) keeps that JS working without
# surgery while the visible UI is gone.
_SERVICE_HQ_FILTER_HTML = """
    <span style="display:none;">
      <input type="checkbox" id="toggle-momentum" />
      <input type="checkbox" id="toggle-septic-blue" />
      <input type="checkbox" id="toggle-eazy-grease" />
    </span>"""


_COLLECTION_FILTER_HTML = """
    <h4>Entity type</h4>
    <label><input type="checkbox" id="toggle-entity-plant" checked />
      ● Processing Plants
      <span id="ent-cnt-plant" class="muted" style="font-weight:normal;"></span></label>
    <label><input type="checkbox" id="toggle-entity-collection" />
      ◆ Collection Operators
      <span id="ent-cnt-collection" class="muted" style="font-weight:normal;"></span></label>
    <label><input type="checkbox" id="toggle-entity-wwtp" />
      ◇ Municipal WWTPs
      <span id="ent-cnt-wwtp" class="muted" style="font-weight:normal;"></span></label>
    <!-- The Collection-Operator filter UI from earlier iterations
         (master + per-bucket sub-toggles) is replaced by the unified
         Ownership filter below. The hidden master is still here so the
         existing collectionClusters event handler keeps working — the
         entity-type master adds/removes clusters via that path. -->
    <span style="display:none;">
      <input type="checkbox" id="toggle-collection-master" />
    </span>
    <div id="coll-sub-toggles" style="display:none;"></div>
"""


_LEGACY_ENTITY_MODE_RX = re.compile(
    r"\s*<h4>Facility type</h4>\s*"
    r"(?:<label[^>]*>[^<]*<input[^>]*name=\"entity-mode\"[^>]*/>[^<]*</label>\s*){3}"
    r"<label[^>]*><input[^>]*id=\"toggle-pumpers-lowzoom\"[^>]*/>[^<]*</label>\s*"
    r"<div class=\"muted\">[^<]*</div>",
    re.DOTALL,
)

# Hidden form controls — the original map's JS still reads these element
# IDs / radios in 6 places (cluster show/hide on tab/zoom/state-filter
# changes). Keep them invisible-but-present with defaults that match the
# old "Both" + "low-zoom-pumpers-hidden" behavior. The visible UI for
# this section is now the separate layer toggles for Processing Plants
# and Collection Operators (added in later patches).
_LEGACY_HIDDEN_FORM = """
    <!-- Legacy controls hidden — replaced by the per-layer toggles below.
         Kept in the DOM so the existing map JS keeps working. -->
    <span style="display:none;">
      <input type="radio" name="entity-mode" value="both" checked />
      <input type="radio" name="entity-mode" value="plant" />
      <input type="radio" name="entity-mode" value="pumper" />
      <input type="checkbox" id="toggle-pumpers-lowzoom" />
    </span>"""


_LEGACY_LAYERS_RX = re.compile(
    r"\s*<h4>Layers</h4>\s*"
    r"<label[^>]*><input[^>]*id=\"toggle-wwtp\"[^>]*/>[^<]*</label>\s*"
    r"<div class=\"muted\">[^<]*</div>\s*"
    r"<label[^>]*><input[^>]*id=\"toggle-tier2\"[^>]*/>[^<]*</label>",
    re.DOTALL,
)

# Hidden-form replacement for the legacy "Layers" section. The IDs
# toggle-wwtp and toggle-tier2 must remain in the DOM because the
# upstream map JS still attaches change handlers to them. We move the
# visible Tier 2 toggle into our Overlays section below.
_LEGACY_LAYERS_HIDDEN = """
    <span style="display:none;">
      <input type="checkbox" id="toggle-wwtp" />
      <input type="checkbox" id="toggle-tier2" />
    </span>"""

_OVERLAYS_HTML = """
    <h4>Overlays</h4>
    <label><input type="checkbox" id="toggle-tier2-visible" /> Tier 2 target markets (50-mi radius)</label>
"""


def patch_filter_panel(body_inner: str) -> str:
    """1. Strip the legacy 'Facility type' radio buttons + the
       toggle-pumpers-lowzoom checkbox (no longer functional now that
       processing plants and collection operators are separate layers).
       2. Strip the legacy 'Layers' section (the duplicate WWTP toggle
       was bypassing the new Entity Type section). Tier 2 moves into a
       new Overlays section below.
       3. Insert the Entity Type section + Overlays right before the
       Base map section."""
    body_inner, n = _LEGACY_ENTITY_MODE_RX.subn(_LEGACY_HIDDEN_FORM, body_inner, count=1)
    if n != 1:
        sys.stderr.write("WARNING: legacy Facility type block not found\n")
    body_inner, n = _LEGACY_LAYERS_RX.subn(_LEGACY_LAYERS_HIDDEN, body_inner, count=1)
    if n != 1:
        sys.stderr.write("WARNING: legacy Layers block not found\n")
    body_inner = body_inner.replace(
        "<h4>Base map</h4>",
        _COLLECTION_FILTER_HTML + _OVERLAYS_HTML + _SERVICE_HQ_FILTER_HTML + "\n    <h4>Base map</h4>",
        1,
    )
    return body_inner


def build_service_hq_script() -> str:
    """Return the JS payload that creates the service-HQ markers and
    wires their checkboxes. Injected near the end of the map's
    <script> block, after `map` is initialized."""
    payload = json.dumps(SERVICE_HQ_DATA)
    return f"""
// ---------- Collection-only platform markers (diamond markers) ----------
// These companies acquire in the FOG space but don't own processing
// plants. Plotted as diamond markers in a separate L.layerGroup per
// company so each filter checkbox controls its own set, all hidden by
// default.
const SERVICE_HQ_DATA = {payload};
const serviceHqLayers = {{
  'momentum':     L.layerGroup(),
  'septic-blue':  L.layerGroup(),
  'eazy-grease':  L.layerGroup()
}};
function _buildServiceHqPopup(d) {{
  return '<div class="service-hq-popup">' +
    '<b>◆ ' + d.company + '</b><br>' +
    '<span style="color:#888; font-size:12px;">Collection / Service HQ — No Owned Plant</span><br>' +
    '<hr style="margin:4px 0;">' +
    '<b>Sponsor:</b> ' + d.sponsor + '<br>' +
    '<b>Service Area:</b> ' + d.city + (d.primary ? ' (primary)' : '') + '<br>' +
    '<b>Focus:</b> ' + d.description + '<br>' +
    '<b>Acquisitions:</b> ' + d.acq_count + ' deals tracked<br>' +
    '<hr style="margin:4px 0;">' +
    '<i>This company provides collection services but does not own processing infrastructure in this market.</i>' +
    '</div>';
}}
SERVICE_HQ_DATA.forEach(function(d) {{
  const icon = L.divIcon({{
    className: 'service-hq-marker',
    html: '<div class="diamond-marker" style="background:' + d.color + ';"></div>',
    iconSize: [12, 12], iconAnchor: [6, 6]
  }});
  const m = L.marker([d.lat, d.lng], {{icon: icon, title: d.company + ' — ' + d.city}});
  m.bindPopup(_buildServiceHqPopup(d), {{maxWidth: 320}});
  serviceHqLayers[d.key].addLayer(m);
}});
['momentum', 'septic-blue', 'eazy-grease'].forEach(function(k) {{
  const cb = document.getElementById('toggle-' + k);
  if (!cb) return;
  cb.addEventListener('change', function() {{
    if (cb.checked) map.addLayer(serviceHqLayers[k]);
    else            map.removeLayer(serviceHqLayers[k]);
  }});
}});
"""


def inject_service_hq(scripts: str) -> str:
    """Append the service-HQ marker setup just before the last </script>."""
    last = scripts.rfind("</script>")
    return scripts[:last] + build_service_hq_script() + scripts[last:]


def build_entity_type_script() -> str:
    """JS that wires the three Entity Type master toggles by hooking into
    the upstream map's existing filter pipeline.

    Strategy:
      * Plants master toggle: monkey-patch upstream `entityVisible(ent)` so
        it returns false when the plants master is off. Cascades cleanly
        through the upstream `refreshCategory` -> shouldShow path: every
        existing path that consults entity visibility now also consults
        the master toggle, with no surgery on the upstream code.
      * Collection master toggle: drives a single
        `applyCollectionFilter()` that adds/removes collectionClusters
        based on master AND-ed with the ownership cb state.
      * Unified ownership: monkey-patch `refreshAllCategories` (the entry
        point used by All/None buttons + initial state) to also call
        `applyCollectionFilter()`, and listen for change events on the
        per-cat owner checkboxes (id="cb-LES" etc) for individual
        toggles.
      * WWTP master toggle: mirrors the existing #toggle-wwtp checkbox.
      * Legend counter (Visible: X plants, Y operators) updates after
        every filter change.
    """
    return """
// ---------- Entity-type master toggles + unified ownership wiring ----------
(function() {
  if (typeof entityVisible !== 'function' || typeof refreshAllCategories !== 'function') {
    console.error('Entity-type wiring: upstream functions missing'); return;
  }
  var plantCb = document.getElementById('toggle-entity-plant');
  var collCb  = document.getElementById('toggle-entity-collection');
  var wwtpCb  = document.getElementById('toggle-entity-wwtp');

  // 1) Plant master via entityVisible patch — cleanest path, makes the
  //    master toggle cascade through ALL upstream code that gates on
  //    entity visibility (refreshCategory, visibleCount, search, etc.).
  var _origEntityVisible = entityVisible;
  window.entityVisible = function(ent) {
    if (plantCb && !plantCb.checked) return false;
    return _origEntityVisible(ent);
  };

  // 2) Collection filter: master AND-ed with each ownership cb state.
  function applyCollectionFilter() {
    var clusters = window.__collectionClusters || {};
    var masterOn = !!(collCb && collCb.checked);
    Object.keys(clusters).forEach(function(key) {
      var c = clusters[key];
      if (!c) return;
      var ownerCb = document.getElementById('cb-' + key);
      var ownerOn = !ownerCb || ownerCb.checked;
      var shouldShow = masterOn && ownerOn;
      if (shouldShow && !map.hasLayer(c)) map.addLayer(c);
      else if (!shouldShow && map.hasLayer(c)) map.removeLayer(c);
    });
  }

  // 3) Legend counter (Visible: X plants, Y operators)
  function updateLegendCounters() {
    var pE = document.getElementById('vis-plants');
    var oE = document.getElementById('vis-ops');
    var plants = 0;
    if (plantCb && plantCb.checked && typeof visibleCount === 'function') {
      plants = visibleCount().plantTotal;
    }
    var ops = 0;
    var clusters = window.__collectionClusters || {};
    Object.keys(clusters).forEach(function(k) {
      var c = clusters[k];
      if (c && map.hasLayer(c) && typeof c.getLayers === 'function') {
        ops += c.getLayers().length;
      }
    });
    if (pE) pE.textContent = plants.toLocaleString();
    if (oE) oE.textContent = ops.toLocaleString();
  }

  // 4) refreshAllCategories monkey-patch — runs after All/None buttons,
  //    state-select changes, etc. so collection layer + counters track.
  var _origRefreshAll = refreshAllCategories;
  window.refreshAllCategories = function() {
    _origRefreshAll();
    applyCollectionFilter();
    updateLegendCounters();
  };

  // 5) Per-cb listener for individual ownership checkbox toggles.
  //    The upstream binds a 'change' on each cb that calls refreshCategory
  //    + updateStats. Our delegated handler fires on bubble after that,
  //    so the upstream side already updated plants by the time this runs.
  document.addEventListener('change', function(ev) {
    var t = ev.target;
    if (t && t.id && t.id.indexOf('cb-') === 0) {
      applyCollectionFilter();
      updateLegendCounters();
    }
  });

  // 6) Master toggle wiring
  if (plantCb) plantCb.addEventListener('change', function() {
    refreshAllCategories();  // patched version handles everything
  });
  if (collCb) collCb.addEventListener('change', function() {
    applyCollectionFilter();
    updateLegendCounters();
  });
  // WWTP toggle drives wwtpCluster directly. Going via the legacy
  // toggle-wwtp checkbox + dispatchEvent triggers the upstream handler,
  // which references DOM elements (legend-wwtp-line, stat-wwtp-status)
  // that were removed in the legend/stats redesign and throws.
  if (wwtpCb) wwtpCb.addEventListener('change', function() {
    if (typeof wwtpCluster === 'undefined') {
      console.warn('wwtpCluster not defined'); return;
    }
    if (wwtpCb.checked && !map.hasLayer(wwtpCluster)) wwtpCluster.addTo(map);
    else if (!wwtpCb.checked && map.hasLayer(wwtpCluster)) map.removeLayer(wwtpCluster);
    // Keep the legacy hidden checkbox in sync (some other code may read it)
    var w = document.getElementById('toggle-wwtp');
    if (w) w.checked = wwtpCb.checked;
  });

  // Tier 2 visible toggle drives tier2Layer directly (legacy
  // toggle-tier2 checkbox stays hidden but in sync).
  var tier2VisibleCb = document.getElementById('toggle-tier2-visible');
  if (tier2VisibleCb) tier2VisibleCb.addEventListener('change', function() {
    if (typeof tier2Layer === 'undefined') return;
    if (tier2VisibleCb.checked) tier2Layer.addTo(map);
    else if (map.hasLayer(tier2Layer)) map.removeLayer(tier2Layer);
    var t = document.getElementById('toggle-tier2');
    if (t) t.checked = tier2VisibleCb.checked;
  });

  // 7) Initial entity-type label counts (static — total dataset sizes)
  function num(n) { return n.toLocaleString(); }
  var fogCount  = (typeof FOG_DATA !== 'undefined')        ? FOG_DATA.length        : 0;
  var collCount = (typeof COLLECTION_DATA !== 'undefined') ? COLLECTION_DATA.length : 0;
  var wwtpCount = (typeof WWTP_DATA !== 'undefined')       ? WWTP_DATA.length       : 0;
  var pe = document.getElementById('ent-cnt-plant');
  var ce = document.getElementById('ent-cnt-collection');
  var we = document.getElementById('ent-cnt-wwtp');
  if (pe) pe.textContent = '(' + num(fogCount) + ')';
  if (ce) ce.textContent = '(' + num(collCount) + ')';
  if (we) we.textContent = '(' + num(wwtpCount) + ')';

  // 8) Initial legend counters (plants visible from default state, ops 0)
  updateLegendCounters();
})();
"""


def inject_entity_type_toggles(scripts: str) -> str:
    last = scripts.rfind("</script>")
    return scripts[:last] + build_entity_type_script() + scripts[last:]


# ============================================================================
# Collection / service operator layer (smaller markers, separate cluster)
# ============================================================================

COLLECTION_JSON = os.path.join(ROOT, "data", "collection_operators.json")


def _load_collection_data() -> list[dict]:
    if not os.path.exists(COLLECTION_JSON):
        sys.stderr.write(f"WARNING: {COLLECTION_JSON} missing — collection layer empty\n")
        return []
    with open(COLLECTION_JSON, encoding="utf-8") as f:
        return json.load(f)


def _bucket_for(o: str) -> str:
    """Map an operator's owner_type string to one of the 9 simplified
    buckets (LES / WRE / BAK / DAR / PUB / PE / REG / LOC / UNK) — same
    keys the plant layer uses, so a single set of ownership checkboxes
    can filter both layers in unison."""
    if not o:
        return "LOC"
    if o.startswith("Wind River"):
        return "WRE"
    if o.startswith("LES"):
        return "LES"
    if "Darling" in o:
        return "DAR"
    if o.startswith("Baker"):
        return "BAK"
    if "Eazy Grease" in o:
        return "REG"
    if o.startswith("Momentum"):
        return "PE"
    if "Septic Blue" in o:
        return "PE"
    if "Barrel" in o:
        return "PUB"
    if "Public:" in o:
        return "PUB"
    if "PE-Backed:" in o or o.startswith("Heritage") or o.startswith("Chuck"):
        return "PE"
    if "Regional:" in o:
        return "REG"
    if "Local:" in o or o == "Independent":
        return "LOC"
    return "PE"


_BUCKET_LABEL = {
    "LES": "LES (Goldman Sachs)",
    "WRE": "Wind River (Gryphon)",
    "BAK": "Baker Commodities",
    "DAR": "Darling / DAR PRO",
    "PUB": "Public Company",
    "PE":  "PE-Backed (Other)",
    "REG": "Regional Operator",
    "LOC": "Local / Family",
    "UNK": "Unknown",
}
_BUCKET_COLOR = {
    "LES": "#e74c3c", "WRE": "#3498db", "BAK": "#27ae60", "DAR": "#f39c12",
    "PUB": "#2C3E50", "PE":  "#c0392b", "REG": "#1abc9c", "LOC": "#95a5a6",
    "UNK": "#bdc3c7",
}


def inject_collection_layer(scripts: str) -> tuple[str, dict[str, int]]:
    data = _load_collection_data()

    # Per-bucket counts kept here for the build-time summary print only.
    # The runtime UI computes its own counts from COLLECTION_DATA so the
    # supplement workflow can splice in updated data without re-running
    # build_index.py.
    counts: dict[str, int] = {}
    for r in data:
        b = _bucket_for(r.get("o") or "")
        counts[b] = counts.get(b, 0) + 1

    payload = json.dumps(data, separators=(",", ":"))
    bucket_meta_static = json.dumps([
        {"key": k, "label": _BUCKET_LABEL[k], "color": _BUCKET_COLOR[k]}
        for k in ["LES", "WRE", "BAK", "DAR", "PUB", "PE", "REG", "LOC", "UNK"]
    ])

    js = f"""
// ---------- Collection / service operator data ----------
// Pumpers, grease-trap haulers, septic services. The marker-comment
// pair below lets scripts/splice_collection.py update this block in
// docs/index.html WITHOUT having to re-run build_index.py from the
// upstream fog_facility_map.html source.
// COLLECTION_DATA_START
const COLLECTION_DATA = {payload};
// COLLECTION_DATA_END

// Static bucket metadata (label + color). Counts are computed at
// runtime from COLLECTION_DATA so they auto-update when the data
// changes.
const COLLECTION_BUCKET_META = {bucket_meta_static};
const collectionClusters = {{}};
const collectionBucketStates = {{}};

(function() {{
  // Compute per-bucket counts and prepare the visible bucket list.
  function bucketKey(o) {{
    if (!o) return 'LOC';
    if (o.indexOf('Wind River') === 0) return 'WRE';
    if (o.indexOf('LES') === 0) return 'LES';
    if (o.indexOf('Darling') >= 0) return 'DAR';
    if (o.indexOf('Baker') === 0) return 'BAK';
    if (o.indexOf('Eazy Grease') >= 0) return 'REG';
    if (o.indexOf('Momentum') === 0) return 'PE';
    if (o.indexOf('Septic Blue') >= 0) return 'PE';
    if (o.indexOf('Barrel') >= 0) return 'PUB';
    if (o.indexOf('Public:') >= 0) return 'PUB';
    if (o.indexOf('PE-Backed:') >= 0) return 'PE';
    if (o.indexOf('Regional:') >= 0) return 'REG';
    if (o.indexOf('Local:') >= 0 || o === 'Independent') return 'LOC';
    return 'PE';
  }}
  const bucketCounts = {{}};
  COLLECTION_DATA.forEach(function(d) {{
    const k = bucketKey(d.o);
    bucketCounts[k] = (bucketCounts[k] || 0) + 1;
  }});
  const COLLECTION_BUCKETS = COLLECTION_BUCKET_META
    .filter(function(b) {{ return (bucketCounts[b.key] || 0) > 0; }})
    .map(function(b) {{
      return {{key: b.key, label: b.label, color: b.color,
              count: bucketCounts[b.key] || 0}};
    }});

  // Build one cluster per bucket so each sub-toggle controls its own.
  COLLECTION_BUCKETS.forEach(function(b) {{
    collectionClusters[b.key] = L.markerClusterGroup({{
      chunkedLoading: true, showCoverageOnHover: false,
      maxClusterRadius: function(zoom) {{
        if (zoom <= 4) return 70;
        if (zoom <= 6) return 55;
        if (zoom <= 8) return 40;
        return 30;
      }},
      disableClusteringAtZoom: 11,
      iconCreateFunction: function(c) {{
        const n = c.getChildCount();
        return L.divIcon({{
          html: '<div style="background:' + b.color + '; opacity:0.8; color:white; '
              + 'width:24px; height:24px; line-height:24px; border-radius:50%; '
              + 'text-align:center; font-weight:600; border:2px solid white; '
              + 'box-shadow:0 0 4px rgba(0,0,0,0.4); font-size:10px">' + n + '</div>',
          className: '', iconSize: L.point(24, 24)
        }});
      }}
    }});
    collectionBucketStates[b.key] = false;
  }});

  function bucketColor(key) {{
    return ({json.dumps(_BUCKET_COLOR)})[key] || '#76D7C4';
  }}

  // Build markers — diamond DivIcon per the unified shape system.
  // Plants stay as CircleMarker (handled by upstream map JS); collection
  // operators are diamonds; WWTPs are the open diamonds the upstream
  // already renders.
  function _diamondHtml(color) {{
    return '<div style="width:10px; height:10px; background:' + color +
      '; transform:rotate(45deg); border:1.5px solid white; ' +
      'box-shadow:0 1px 3px rgba(0,0,0,0.4); opacity:0.85;"></div>';
  }}
  const popupOpenForCollection = {{}};
  COLLECTION_DATA.forEach(function(d) {{
    const key = bucketKey(d.o);
    const cluster = collectionClusters[key];
    if (!cluster) return;
    const icon = L.divIcon({{
      className: 'collection-diamond-icon',
      html: _diamondHtml(bucketColor(key)),
      iconSize: [12, 12],
      iconAnchor: [6, 6],
      popupAnchor: [0, -6]
    }});
    const m = L.marker([d.la, d.lo], {{icon: icon}});
    m.bindPopup(function() {{
      let html = '<div class="collection-popup">' +
        '<b>' + (d.n || '') + '</b>' +
        '<span class="collection-badge">Collection / Service</span><br>';
      if (d.ad) html += (d.ad) + ', ';
      if (d.c) html += d.c + ', ';
      if (d.s) html += d.s;
      if (d.z) html += ' ' + d.z;
      html += '<hr>';
      html += '<b>Owner:</b> ' + (d.op || d.n || '?') + '<br>';
      html += '<b>Type:</b> ' + (d.o || '?') + '<br>';
      if (d.na) html += '<b>NAICS:</b> ' + d.na + '<br>';
      const srcLabel = d.src === 'EPA_FRS_legacy' ? 'EPA FRS' :
                        d.src === 'web_search' ? 'Web search' :
                        d.src === 'EPA_FRS' ? 'EPA FRS' : (d.src || '?');
      html += '<b>Data source:</b> ' + srcLabel + '<br>';
      if (d.np) {{
        html += '<b>Nearest processing plant:</b> ' + d.np;
        if (d.nd != null) html += ' (' + d.nd + ' mi)';
        html += '<br>';
      }}
      if (d.nplo) html += '<b>Plant owner:</b> ' + d.nplo + '<br>';
      html += '</div>';
      return html;
    }}, {{maxWidth: 340}});
    cluster.addLayer(m);
  }});

  // ---------- Filter UI ----------
  const sub = document.getElementById('coll-sub-toggles');
  const totalLabel = document.getElementById('coll-total-count');
  if (totalLabel) totalLabel.textContent = ' (' + COLLECTION_DATA.length.toLocaleString() + ' total)';
  if (sub) {{
    sub.innerHTML = '<div class="muted" style="font-size:11px; margin-bottom:4px;">Filter by type:</div>' +
      COLLECTION_BUCKETS.map(function(b) {{
        return '<label style="font-size:11px;"><input type="checkbox" class="coll-sub" data-key="'
          + b.key + '" checked /> '
          + '<span style="display:inline-block; width:10px; height:10px; border-radius:50%; '
          + 'background:' + b.color + '; margin-right:4px; vertical-align:middle;"></span>'
          + b.label + ' <span class="muted">(' + b.count.toLocaleString() + ')</span></label>';
      }}).join('');
  }}
  function applyMasterAndSubs() {{
    const master = document.getElementById('toggle-collection-master');
    const masterOn = master && master.checked;
    if (sub) sub.style.display = masterOn ? 'block' : 'none';
    COLLECTION_BUCKETS.forEach(function(b) {{
      const cb = document.querySelector('.coll-sub[data-key="' + b.key + '"]');
      const wantOn = masterOn && (!cb || cb.checked);
      const cluster = collectionClusters[b.key];
      if (!cluster) return;
      const isOn = collectionBucketStates[b.key];
      if (wantOn && !isOn) {{ map.addLayer(cluster); collectionBucketStates[b.key] = true; }}
      else if (!wantOn && isOn) {{ map.removeLayer(cluster); collectionBucketStates[b.key] = false; }}
    }});
  }}
  const master = document.getElementById('toggle-collection-master');
  if (master) master.addEventListener('change', applyMasterAndSubs);
  document.querySelectorAll('.coll-sub').forEach(function(cb) {{
    cb.addEventListener('change', applyMasterAndSubs);
  }});

  // ---------- Stats panel injection ----------
  const stats = document.getElementById('stats-panel');
  if (stats) {{
    const distances = COLLECTION_DATA.map(function(d) {{ return d.nd; }})
      .filter(function(d) {{ return typeof d === 'number'; }});
    const avg = distances.length ? (distances.reduce(function(a,b) {{ return a+b; }}, 0) / distances.length) : 0;
    const far = distances.filter(function(d) {{ return d > 25; }}).length;
    const html = '<h4 style="margin-top:8px;">Collection / Service Operators</h4>' +
      '<div class="stats-line"><b>Total:</b> ' + COLLECTION_DATA.length.toLocaleString() + '</div>' +
      COLLECTION_BUCKETS.map(function(b) {{
        return '<div class="stats-line"><b>' + b.label + ':</b> ' + b.count.toLocaleString() + '</div>';
      }}).join('') +
      '<div class="stats-line"><b>Avg distance to nearest plant:</b> ' + avg.toFixed(1) + ' mi</div>' +
      '<div class="stats-line"><b>&gt;25 mi from nearest plant:</b> ' + far.toLocaleString() + '</div>';
    const body = stats.querySelector('.panel-body');
    if (body) body.insertAdjacentHTML('beforeend', html);
  }}

  // Expose for findOnMap nearest-marker iteration
  window.__collectionClusters = collectionClusters;
}})();
"""
    last = scripts.rfind("</script>")
    return scripts[:last] + js + scripts[last:], counts


def patch_collection_legend(body_inner: str) -> str:
    """Add a small-circle legend row for collection operators."""
    row = (
        '\n      <tr><td><span class="swatch" style="width:8px; height:8px; '
        'background:#76D7C4;"></span></td>'
        '<td>Collection / service operator (small marker)</td></tr>'
    )
    return re.sub(
        r'(<tr><td><span class="legend-diamond"[^/]+/></td><td>Collection-only platforms[^<]*</td></tr>)',
        r"\1" + row,
        body_inner,
        count=1,
    )


def patch_facility_data(
    scripts: str,
) -> tuple[str, dict[str, int], dict[str, int], list[dict]]:
    """Find the FOG_DATA literal in the script block, parse it, apply the
    FOG-only filter, run public-company / Eazy Grease reclassification on
    survivors, then strip pumpers (those move to COLLECTION_DATA), and
    write the plants-only result back. Returns (scripts, public_counts,
    filter_counts, kept_records). kept_records is the plants-only
    dataset embedded as FOG_DATA."""
    m = re.search(r"const FOG_DATA = (\[.*?\]);\s*\n", scripts, flags=re.DOTALL)
    if not m:
        sys.stderr.write("WARNING: FOG_DATA literal not found; skipping filter+reclassification\n")
        return scripts, {}, {}, []
    raw = m.group(1)
    records = json.loads(raw)
    before = len(records)

    records, filter_counts = filter_to_fog_only(records)
    filter_counts["_before"] = before
    filter_counts["_after_filter"] = len(records)

    public_counts = reclassify_to_public(records)
    eazy_n = reclassify_eazy_grease(records)
    if eazy_n:
        public_counts["Regional: Eazy Grease (Private)"] = eazy_n

    # Pumpers move to the collection-operator layer (loaded from
    # data/collection_operators.json). Strip them from FOG_DATA so the
    # plants layer is plants-only and visually distinct.
    plants_only = [r for r in records if r.get("e") == "plant"]
    filter_counts["_after"] = len(plants_only)
    filter_counts["_pumpers_moved_to_collection"] = len(records) - len(plants_only)

    new_literal = json.dumps(plants_only, separators=(",", ":"))
    scripts = scripts[: m.start()] + f"const FOG_DATA = {new_literal};\n" + scripts[m.end():]
    return scripts, public_counts, filter_counts, plants_only


TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>FOG Industry Command Center</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" crossorigin=""/>
<link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.css" />
<link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.Default.css" />
<link rel="stylesheet" href="https://unpkg.com/leaflet.fullscreen@3.0.2/Control.FullScreen.css" />
<style>
/* ============ Command center shell ============ */
:root {
  --topbar-bg: #1F3864;
  --topbar-text: #FFFFFF;
  --topbar-text-inactive: #8FAADC;
  --accent: #3498db;
  --target-green: #27ae60;
}
html, body {
  margin: 0; padding: 0; height: 100%;
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  font-size: 13px; color: #222; background: #f6f7f9;
}
#topbar {
  position: fixed; top: 0; left: 0; right: 0;
  height: 52px; z-index: 5000;
  background: var(--topbar-bg); color: var(--topbar-text);
  display: flex; align-items: center; padding: 0 16px;
  box-shadow: 0 2px 8px rgba(0,0,0,0.18);
}
#topbar .title { font-size: 15px; font-weight: 600; letter-spacing: 0.2px; margin-right: 24px; }
#topbar nav.tabs { display: flex; height: 100%; }
.tab-btn {
  background: transparent; border: none; outline: none; cursor: pointer;
  color: var(--topbar-text-inactive);
  font-size: 13px; font-weight: 500;
  padding: 0 18px; height: 100%;
  border-bottom: 3px solid transparent;
  transition: color 0.12s, border-color 0.12s;
  font-family: inherit;
}
.tab-btn:hover { color: #fff; }
.tab-btn.active { color: #fff; border-bottom-color: var(--accent); }
#topbar .meta { margin-left: auto; font-size: 11px; color: var(--topbar-text-inactive); }
#topbar .meta b { color: #fff; font-weight: 600; }

.tab-content {
  display: none;
  position: absolute; top: 52px; left: 0; right: 0; bottom: 0;
  overflow: auto;
}
.tab-content.active { display: block; }
#content-map.tab-content { overflow: hidden; }

/* ============ News feed ============ */
#content-news .inner { padding: 16px; max-width: 920px; margin: 0 auto; }
.news-filter-bar {
  display: flex; gap: 8px; flex-wrap: wrap; align-items: center;
  margin-bottom: 16px;
}
.pill {
  display: inline-block; padding: 5px 12px; border: 1px solid #ccc;
  border-radius: 999px; font-size: 12px; cursor: pointer; user-select: none;
  background: #fff; color: #333;
}
.pill:hover { background: #eef; }
.pill.active { background: #1F3864; color: #fff; border-color: #1F3864; }
.search-box {
  padding: 6px 10px; border: 1px solid #ccc; border-radius: 4px; font-size: 13px;
  flex: 1; min-width: 200px;
  font-family: inherit;
}
.news-card {
  background: #fff; border: 1px solid #e2e2e2; border-left-width: 4px;
  border-radius: 4px; padding: 12px 14px; margin-bottom: 10px;
}
.news-card.cat-MA            { border-left-color: #e74c3c; }
.news-card.cat-Regulatory    { border-left-color: #8E44AD; }
.news-card.cat-RenewableFuels{ border-left-color: #27ae60; }
.news-card.cat-PublicCo      { border-left-color: #2C3E50; }
.news-card.cat-Restaurant    { border-left-color: #f39c12; }
.news-card.cat-Technology    { border-left-color: #3498db; }
.news-card.cat-LaborOps      { border-left-color: #95a5a6; }
.news-card.cat-Infrastructure{ border-left-color: #1abc9c; }
.news-card.cat-IndustryEvents{ border-left-color: #d35400; }
.news-card.cat-ESG           { border-left-color: #16a085; }
.news-card .meta { font-size: 11px; color: #888; margin-bottom: 4px; }
.news-card .meta .cat-tag {
  display: inline-block; padding: 2px 7px; border-radius: 3px;
  font-size: 10px; font-weight: 700; margin-right: 8px; color: #fff;
  letter-spacing: 0.3px;
}
.news-card .meta .relevance-badge {
  display: inline-block; padding: 2px 7px; border-radius: 3px;
  font-size: 10px; font-weight: 700; margin-left: 6px;
  background: #FFF4E5; color: #B8550A; border: 1px solid #F5C77A;
}
.news-card .headline { font-size: 14px; font-weight: 600; margin-bottom: 6px; line-height: 1.35; }
.news-card .summary { color: #444; line-height: 1.5; margin-bottom: 6px; }
.news-card .source-link { font-size: 12px; color: #1F3864; text-decoration: none; }
.news-card .source-link:hover { text-decoration: underline; }
.news-card .target-alert {
  margin-top: 8px; padding: 6px 8px; background: #FDE8E8; border-radius: 3px;
  font-size: 12px; font-weight: 600; color: #b03030;
}
#news-load-more {
  display: block; margin: 12px auto; padding: 8px 18px;
  background: #fff; border: 1px solid #ccc; border-radius: 4px;
  cursor: pointer; font-family: inherit; font-size: 12px;
}
#news-load-more:hover { background: #eef; }
#news-count { font-size: 11px; color: #888; margin-bottom: 8px; }

/* ============ Transaction comps ============ */
#content-comps .inner { padding: 16px; }
.summary-bar {
  background: #fff; border: 1px solid #e2e2e2; border-radius: 4px;
  padding: 10px 14px; margin-bottom: 12px; font-size: 13px;
}
.summary-bar .stat { margin-right: 18px; display: inline-block; }
.summary-bar .stat b { color: #1F3864; }
.controls-row {
  display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 12px;
  align-items: center;
}
.controls-row select, .controls-row input[type=text] {
  padding: 6px 8px; border: 1px solid #ccc; border-radius: 4px; font-size: 13px;
  font-family: inherit;
}
.controls-row label { font-size: 12px; user-select: none; }
.export-btn {
  margin-left: auto; padding: 7px 14px; background: #1F3864; color: #fff;
  border: none; border-radius: 4px; cursor: pointer; font-size: 12px;
  font-family: inherit;
}
.export-btn:hover { background: #16294a; }
.comps-table-wrap {
  overflow-x: auto; background: #fff; border-radius: 4px; border: 1px solid #e2e2e2;
}
.comps-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.comps-table th {
  background: #f3f5f8; padding: 8px 10px; text-align: left;
  border-bottom: 2px solid #d8d8d8; cursor: pointer; user-select: none;
  font-weight: 600; white-space: nowrap;
}
.comps-table th:hover { background: #e9ecf1; }
.comps-table th .arrow { font-size: 10px; color: #888; margin-left: 3px; }
.comps-table td { padding: 6px 10px; border-bottom: 1px solid #eee; vertical-align: top; }
.comps-table tr.row-target-market td:first-child { border-left: 3px solid var(--target-green); }
.comps-table tbody tr:not(.expanded-row):nth-of-type(2n) td { background: #fafbfc; }
.comps-table tr.expanded-row td { background: #f6f9fc !important; }
.comps-table .detail-cell { padding: 12px 14px; line-height: 1.55; }
.comps-table .row-clickable { cursor: pointer; }
.comps-table .row-clickable:hover td { background: #eef3f8 !important; }
#row-count { font-size: 11px; color: #777; margin-top: 6px; }

/* Find-on-Map button (transactions tab) */
.find-on-map-btn {
  margin-top: 12px;
  padding: 8px 20px;
  background: #1F3864;
  color: white;
  border: none;
  border-radius: 6px;
  cursor: pointer;
  font-size: 14px;
  font-weight: 600;
  transition: background 0.2s;
  font-family: inherit;
}
.find-on-map-btn:hover { background: #2E86C1; }
.find-on-map-btn:disabled { background: #aaa; cursor: not-allowed; }
.find-on-map-link {
  display: inline-block; margin-top: 8px;
  padding: 5px 12px; border-radius: 4px;
  background: #1F3864; color: white;
  font-size: 11px; font-weight: 600;
  text-decoration: none; cursor: pointer;
}
.find-on-map-link:hover { background: #2E86C1; }

/* Pulsing highlight circle on the map */
.pulse-circle { animation: pulse-opacity 2s ease-in-out infinite; }
@keyframes pulse-opacity {
  0%, 100% { opacity: 0.6; }
  50%      { opacity: 1.0; }
}

/* Collection / Service operator markers (small diamonds) */
.collection-diamond-icon { background: transparent !important; border: none !important; }
.collection-popup hr { border: 0; border-top: 1px solid #ddd; margin: 6px 0; }
.collection-badge {
  display: inline-block; margin-left: 6px;
  padding: 1px 6px; border-radius: 3px;
  background: #76D7C4; color: #1F3864;
  font-size: 10px; font-weight: 700;
  letter-spacing: 0.3px;
}

/* Service-HQ markers (collection-only platforms, hidden by default) */
.service-hq-marker { background: transparent !important; border: none !important; }
.diamond-marker {
  width: 10px; height: 10px;
  transform: rotate(45deg);
  border: 2px solid white;
  box-shadow: 0 1px 4px rgba(0,0,0,0.4);
}
.hq-swatch {
  display: inline-block; width: 10px; height: 10px;
  transform: rotate(45deg);
  border: 1.5px solid #555;
  margin: 0 4px 0 2px; vertical-align: middle;
}
.legend-diamond {
  display: inline-block; width: 10px; height: 10px;
  transform: rotate(45deg);
  border: 1.5px solid #555;
  margin: 0 6px 0 2px; vertical-align: middle;
}

/* Deal label popup floating over the map */
.deal-label-popup .leaflet-popup-content-wrapper {
  background: #1F3864; color: white;
  border-radius: 8px; font-size: 13px;
  box-shadow: 0 4px 12px rgba(0,0,0,0.3);
}
.deal-label-popup .leaflet-popup-tip { background: #1F3864; }
.deal-label-popup .leaflet-popup-close-button { color: #fff !important; }
.deal-label-popup .leaflet-popup-content { color: #fff; }
.deal-map-label { padding: 4px 8px; line-height: 1.5; }

/* ============ Original map styles (scoped to map tab) ============ */
__ORIGINAL_STYLE__
</style>
</head>
<body>

<header id="topbar">
  <span class="title">FOG Industry Command Center</span>
  <nav class="tabs">
    <button class="tab-btn" data-tab="news">📰 News</button>
    <button class="tab-btn" data-tab="comps">📊 Transactions</button>
    <button class="tab-btn active" data-tab="map">🗺️ Map</button>
  </nav>
  <span class="meta">
    Last refreshed: <b id="meta-refreshed">—</b>
    &nbsp;·&nbsp; Deals: <b id="meta-deals">—</b>
    &nbsp;·&nbsp; News: <b id="meta-news">—</b>
  </span>
</header>

<!-- ============ Tab 1: News Feed ============ -->
<section id="content-news" class="tab-content">
  <div class="inner">
    <div class="news-filter-bar">
      <span class="pill news-pill active" data-cat="All">All</span>
      <span class="pill news-pill" data-cat="M&A">M&amp;A</span>
      <span class="pill news-pill" data-cat="Regulatory">Regulatory</span>
      <span class="pill news-pill" data-cat="Renewable Fuels">Renewable Fuels</span>
      <span class="pill news-pill" data-cat="Public Co.">Public Co.</span>
      <span class="pill news-pill" data-cat="Restaurant">Restaurant</span>
      <span class="pill news-pill" data-cat="Technology">Technology</span>
      <span class="pill news-pill" data-cat="Labor/Ops">Labor/Ops</span>
      <span class="pill news-pill" data-cat="Infrastructure">Infrastructure</span>
      <span class="pill news-pill" data-cat="Industry Events">Industry Events</span>
      <span class="pill news-pill" data-cat="ESG">ESG</span>
      <input id="news-search" class="search-box" type="text" placeholder="Search headlines..." />
    </div>
    <div id="news-count"></div>
    <div id="news-list"></div>
    <button id="news-load-more" style="display:none;">Load more</button>
  </div>
</section>

<!-- ============ Tab 2: Transaction Comps ============ -->
<section id="content-comps" class="tab-content">
  <div class="inner">
    <div id="comp-summary" class="summary-bar"></div>
    <div class="controls-row">
      <select id="comp-platform">
        <option value="All">All Platforms</option>
        <option>LES</option>
        <option>Wind River</option>
        <option>Darling</option>
        <option>Baker</option>
        <option>Other</option>
      </select>
      <select id="comp-deal-type">
        <option value="All">All Deal Types</option>
        <option>Platform</option>
        <option>Add-On</option>
        <option>Strategic</option>
        <option>Division Sale</option>
      </select>
      <select id="comp-year">
        <option value="All">All Years</option>
        <option>Earlier</option>
      </select>
      <label><input type="checkbox" id="comp-target-only" /> Target markets only</label>
      <input id="comp-search" type="text" placeholder="Search..." />
      <button id="comp-export" class="export-btn">📥 Download CSV</button>
    </div>
    <div class="comps-table-wrap">
      <table class="comps-table">
        <thead>
          <tr>
            <th data-sort="date">Date <span class="arrow">↕</span></th>
            <th data-sort="target">Target <span class="arrow">↕</span></th>
            <th data-sort="acquirer">Acquirer <span class="arrow">↕</span></th>
            <th data-sort="sponsor">Sponsor <span class="arrow">↕</span></th>
            <th data-sort="location">Location <span class="arrow">↕</span></th>
            <th data-sort="deal_type">Type <span class="arrow">↕</span></th>
            <th data-sort="deal_size">Deal Size <span class="arrow">↕</span></th>
            <th data-sort="multiple">EV/EBITDA <span class="arrow">↕</span></th>
            <th data-sort="services">Services <span class="arrow">↕</span></th>
          </tr>
        </thead>
        <tbody id="comps-tbody"></tbody>
      </table>
    </div>
    <div id="row-count"></div>
  </div>
</section>

<!-- ============ Tab 3: Interactive Map ============ -->
<section id="content-map" class="tab-content active">
__BODY_MAP_INNER__
</section>

<!-- ============ Embedded data blocks (refreshed daily) ============ -->
<!-- METADATA_START -->
<script>
const metadata = __METADATA_JSON__;
</script>
<!-- METADATA_END -->

<!-- NEWS_FEED_DATA_START -->
<script>
const newsFeedData = __NEWS_FEED_JSON__;
</script>
<!-- NEWS_FEED_DATA_END -->

<!-- COMP_DATABASE_DATA_START -->
<script>
const compDatabaseData = __COMP_DATABASE_JSON__;
</script>
<!-- COMP_DATABASE_DATA_END -->

<!-- ============ Original map scripts (Leaflet + FOG_DATA + WWTP_DATA + init) ============ -->
__MAP_SCRIPTS__

<!-- ============ Command-center shell scripts ============ -->
<script>
(function () {
  // ---------- Tab switcher ----------
  function switchTab(tabName) {
    document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
    document.getElementById('content-' + tabName).classList.add('active');
    document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));
    document.querySelector('[data-tab="' + tabName + '"]').classList.add('active');
    if (tabName === 'map' && window.__fogMap) {
      setTimeout(function () { window.__fogMap.invalidateSize(); }, 150);
    }
  }
  document.querySelectorAll('.tab-btn').forEach(function (btn) {
    btn.addEventListener('click', function () { switchTab(btn.dataset.tab); });
  });

  // ---------- Delegated click handler for Find-on-Map buttons/links ----------
  // Buttons/links are rendered dynamically (each render rebuilds tbody and
  // news-list innerHTML), so a single delegated listener on document covers
  // all current and future renders.
  document.addEventListener('click', function (ev) {
    var el = ev.target;
    while (el && el !== document) {
      if (el.classList && (el.classList.contains('find-on-map-btn') ||
                            el.classList.contains('find-on-map-link'))) {
        ev.preventDefault();
        ev.stopPropagation();
        var lat = parseFloat(el.dataset.lat);
        var lng = parseFloat(el.dataset.lng);
        if (isNaN(lat) || isNaN(lng)) return;
        window.findOnMap(lat, lng,
          el.dataset.target || '',
          el.dataset.acquirer || '',
          el.dataset.date || '',
          el.dataset.zoom || 'narrow');
        return;
      }
      el = el.parentNode;
    }
  });

  // ---------- Find-on-Map (transactions tab → map tab) ----------
  // Exposed globally because comp-row inline onclick handlers call it.
  window.findOnMap = function (lat, lng, target, acquirer, date, zoomHint) {
    if (typeof lat !== 'number' || typeof lng !== 'number') return;
    switchTab('map');

    var attempts = 0;
    var MAX_ATTEMPTS = 4;
    function go() {
      var fogMap = window.__fogMap;
      var clusters = window.__fogClusters;
      var markers = window.__fogMarkers;
      var catOrder = window.__fogCatOrder;
      if (!fogMap) {
        if (++attempts < MAX_ATTEMPTS) return setTimeout(go, 500);
        return;
      }
      fogMap.invalidateSize();

      var zoom = (zoomHint === 'wide') ? 5 : 12;
      fogMap.setView([lat, lng], zoom, { animate: true });

      // Pulsing 25-mile highlight circle
      var highlight = L.circle([lat, lng], {
        radius: 40234,
        color: '#e74c3c',
        fillColor: '#e74c3c',
        fillOpacity: 0.08,
        weight: 2,
        dashArray: '8, 8',
        className: 'pulse-circle'
      }).addTo(fogMap);
      setTimeout(function () { fogMap.removeLayer(highlight); }, 15000);

      // Floating deal label
      var labelHtml = '<div class="deal-map-label">📍 <b>' +
        escapeHtml(target) + '</b><br>' +
        (acquirer === 'news' ? '' : 'Acquired by ' + escapeHtml(acquirer) + ' ') +
        '(' + escapeHtml(date) + ')</div>';
      var label = L.popup({
        closeButton: true, autoClose: false, closeOnClick: true,
        className: 'deal-label-popup', offset: [0, -20]
      }).setLatLng([lat, lng]).setContent(labelHtml).openOn(fogMap);
      setTimeout(function () { fogMap.closePopup(label); }, 8000);

      // Skip nearest-marker step on wide-zoom (multi-region) views.
      if (zoomHint === 'wide' || !clusters || !markers || !catOrder) return;

      // Find nearest marker across all FOG (category, entity) buckets and
      // (if loaded) the collection-operator clusters too.
      var nearest = null, nearestDist = Infinity, nearestCluster = null;
      catOrder.forEach(function (cat) {
        ['plant', 'pumper'].forEach(function (ent) {
          var arr = (markers[cat] && markers[cat][ent]) || [];
          for (var i = 0; i < arr.length; i++) {
            var m = arr[i];
            if (!m.getLatLng) continue;
            var d = fogMap.distance([lat, lng], m.getLatLng());
            if (d < nearestDist) {
              nearestDist = d;
              nearest = m;
              nearestCluster = clusters[cat] && clusters[cat][ent];
            }
          }
        });
      });
      var collClusters = window.__collectionClusters || {};
      Object.keys(collClusters).forEach(function (key) {
        var cluster = collClusters[key];
        if (!cluster || typeof cluster.eachLayer !== 'function') return;
        cluster.eachLayer(function (m) {
          if (!m.getLatLng) return;
          var d = fogMap.distance([lat, lng], m.getLatLng());
          if (d < nearestDist) {
            nearestDist = d;
            nearest = m;
            nearestCluster = cluster;
          }
        });
      });
      // Open popup if within 10 miles (16,093 m). Use cluster.zoomToShowLayer
      // to spiderfy if the marker is currently inside a cluster.
      if (nearest && nearestDist < 16093 && nearestCluster &&
          typeof nearestCluster.zoomToShowLayer === 'function') {
        nearestCluster.zoomToShowLayer(nearest, function () {
          try { nearest.openPopup(); } catch (_) {}
        });
      } else if (nearest && nearestDist < 16093) {
        try { nearest.openPopup(); } catch (_) {}
      }
    }
    setTimeout(go, 200);
  };

  // ---------- Helpers ----------
  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c];
    });
  }
  // Strip any <tag> / </tag> fragments before display. Belt-and-suspenders
  // alongside the Python clean_summary() at ingestion — catches any
  // citation/markup that slipped past or arrives in pre-existing data.
  function sanitizeText(text) {
    if (!text) return '';
    return String(text).replace(/<\/?[^>]+(>|$)/g, '').replace(/\s+/g, ' ').trim();
  }
  function formatDate(s) {
    if (!s) return '';
    var d = new Date(s + (s.length === 10 ? 'T00:00:00' : ''));
    if (isNaN(d.getTime())) return s;
    return d.toLocaleDateString('en-US', { year: 'numeric', month: 'short', day: 'numeric' });
  }
  function normCat(s) {
    return String(s || '').toLowerCase().replace(/\s+/g, '').replace(/&/g, 'and');
  }

  // ---------- Metadata header ----------
  if (typeof metadata !== 'undefined') {
    var d = metadata.lastRefreshed ? new Date(metadata.lastRefreshed) : null;
    document.getElementById('meta-refreshed').textContent =
      d && !isNaN(d.getTime()) ? d.toLocaleString() : (metadata.lastRefreshed || '—');
    document.getElementById('meta-deals').textContent =
      (typeof compDatabaseData !== 'undefined') ? compDatabaseData.length : (metadata.compCount || '—');
    document.getElementById('meta-news').textContent =
      (typeof newsFeedData !== 'undefined') ? newsFeedData.length : (metadata.newsCount || '—');
  }

  // ---------- News tab ----------
  var newsActiveCategory = 'All';
  var newsSearch = '';
  var newsLimit = 100;

  function renderNews() {
    var container = document.getElementById('news-list');
    var data = (typeof newsFeedData !== 'undefined') ? newsFeedData : [];
    var items = data
      .filter(function (n) { return newsActiveCategory === 'All' || normCat(n.category) === normCat(newsActiveCategory); })
      .filter(function (n) {
        if (!newsSearch) return true;
        var q = newsSearch.toLowerCase();
        return (n.headline || '').toLowerCase().indexOf(q) >= 0 ||
               (n.summary || '').toLowerCase().indexOf(q) >= 0;
      })
      .sort(function (a, b) { return (b.date || '').localeCompare(a.date || ''); });

    var visible = items.slice(0, newsLimit);
    if (visible.length === 0) {
      container.innerHTML = '<div style="text-align:center; padding:40px; color:#888;">News feed is being populated. Data refreshes daily at 7 AM CT.</div>';
      document.getElementById('news-count').textContent = '0 items';
      document.getElementById('news-load-more').style.display = 'none';
      return;
    }

    var catBg = {
      'M&A': '#e74c3c',
      'Regulatory': '#8E44AD',
      'Renewable Fuels': '#27ae60',
      'Public Co.': '#2C3E50',
      'Restaurant': '#f39c12',
      'Technology': '#3498db',
      'Labor/Ops': '#95a5a6',
      'Infrastructure': '#1abc9c',
      'Industry Events': '#d35400',
      'ESG': '#16a085'
    };
    var catCls = {
      'M&A': 'cat-MA',
      'Regulatory': 'cat-Regulatory',
      'Renewable Fuels': 'cat-RenewableFuels',
      'Public Co.': 'cat-PublicCo',
      'Restaurant': 'cat-Restaurant',
      'Technology': 'cat-Technology',
      'Labor/Ops': 'cat-LaborOps',
      'Infrastructure': 'cat-Infrastructure',
      'Industry Events': 'cat-IndustryEvents',
      'ESG': 'cat-ESG'
    };
    container.innerHTML = visible.map(function (n) {
      var cat = n.category || 'Industry Events';
      var bg = catBg[cat] || '#888';
      var cls = catCls[cat] || 'cat-IndustryEvents';
      var alert = n.is_target_market
        ? '<div class="target-alert">⚠️ TIER 2 ALERT: near ' + escapeHtml(n.target_market_name || 'target market') + '</div>'
        : '';
      var src = n.source_url
        ? '<a class="source-link" href="' + escapeHtml(n.source_url) + '" target="_blank" rel="noopener">Source: ' + escapeHtml(n.source || 'link') + ' →</a>'
        : (n.source ? '<span class="source-link">Source: ' + escapeHtml(n.source) + '</span>' : '');
      var relevance = parseInt(n.relevance_score, 10);
      var relBadge = (!isNaN(relevance) && relevance >= 4)
        ? '<span class="relevance-badge" title="High relevance to FOG roll-up strategy">🔥 High Relevance</span>'
        : '';
      var mapLink = '';
      if ((cat === 'M&A' || n.is_deal) &&
          typeof n.latitude === 'number' && typeof n.longitude === 'number') {
        mapLink = '<a class="find-on-map-link" href="#"' +
          ' data-lat="' + n.latitude + '"' +
          ' data-lng="' + n.longitude + '"' +
          ' data-target="' + escapeHtml(n.headline || '') + '"' +
          ' data-acquirer="news"' +
          ' data-date="' + escapeHtml(n.date || '') + '"' +
          ' data-zoom="' + escapeHtml(n.zoom_hint || 'narrow') + '"' +
          '>🗺️ Find on Map</a>';
      }
      return '<div class="news-card ' + cls + '">' +
        '<div class="meta"><span class="cat-tag" style="background:' + bg + '">' + escapeHtml(cat) + '</span>' + escapeHtml(formatDate(n.date)) + relBadge + '</div>' +
        '<div class="headline">' + escapeHtml(sanitizeText(n.headline || '')) + '</div>' +
        '<div class="summary">' + escapeHtml(sanitizeText(n.summary || '')) + '</div>' +
        src + alert +
        (mapLink ? '<div>' + mapLink + '</div>' : '') +
        '</div>';
    }).join('');

    document.getElementById('news-count').textContent = 'Showing ' + visible.length + ' of ' + items.length + ' items';
    var moreBtn = document.getElementById('news-load-more');
    if (items.length > visible.length) {
      moreBtn.style.display = 'block';
      moreBtn.textContent = 'Load more (' + (items.length - visible.length) + ' older)';
    } else {
      moreBtn.style.display = 'none';
    }
  }

  document.querySelectorAll('.news-pill').forEach(function (p) {
    p.addEventListener('click', function () {
      newsActiveCategory = p.dataset.cat;
      document.querySelectorAll('.news-pill').forEach(function (x) { x.classList.toggle('active', x === p); });
      newsLimit = 100;
      renderNews();
    });
  });
  document.getElementById('news-search').addEventListener('input', function (e) {
    newsSearch = e.target.value; renderNews();
  });
  document.getElementById('news-load-more').addEventListener('click', function () {
    newsLimit += 100; renderNews();
  });

  // ---------- Comps tab ----------
  var compsState = {
    platform: 'All', dealType: 'All', year: 'All', targetOnly: false, search: '',
    sortKey: 'date', sortDir: 'desc',
    expandedRows: new Set()
  };

  function platformOf(deal) {
    var a = (deal.acquirer || '').toLowerCase();
    if (a.indexOf('wind river') >= 0) return 'Wind River';
    if (a.indexOf('liquid environmental') >= 0 || a.indexOf('(les)') >= 0 || /^les\b/.test(a)) return 'LES';
    if (a.indexOf('darling') >= 0) return 'Darling';
    if (a.indexOf('baker') >= 0) return 'Baker';
    return 'Other';
  }
  function compYear(d) { return (d.date || '').slice(0, 4); }

  function filteredComps() {
    var data = (typeof compDatabaseData !== 'undefined') ? compDatabaseData : [];
    return data.filter(function (d) {
      if (compsState.platform !== 'All' && platformOf(d) !== compsState.platform) return false;
      if (compsState.dealType !== 'All' && d.deal_type !== compsState.dealType) return false;
      if (compsState.year !== 'All') {
        if (compsState.year === 'Earlier') {
          if (parseInt(compYear(d), 10) >= 2020) return false;
        } else if (compYear(d) !== compsState.year) return false;
      }
      if (compsState.targetOnly && !d.is_target_market) return false;
      if (compsState.search) {
        var q = compsState.search.toLowerCase();
        var hay = [d.target, d.acquirer, d.sponsor, d.location, d.services, d.notes].join(' ').toLowerCase();
        if (hay.indexOf(q) < 0) return false;
      }
      return true;
    }).sort(function (a, b) {
      var k = compsState.sortKey;
      var av = a[k] == null ? '' : String(a[k]);
      var bv = b[k] == null ? '' : String(b[k]);
      if (av < bv) return compsState.sortDir === 'asc' ? -1 : 1;
      if (av > bv) return compsState.sortDir === 'asc' ? 1 : -1;
      return 0;
    });
  }

  function renderComps() {
    var data = (typeof compDatabaseData !== 'undefined') ? compDatabaseData : [];
    var total = data.length;
    var platforms = data.filter(function (d) { return d.deal_type === 'Platform'; }).length;
    var addons = data.filter(function (d) { return d.deal_type === 'Add-On'; }).length;
    var others = total - platforms - addons;
    var tm = data.filter(function (d) { return d.is_target_market; }).length;
    document.getElementById('comp-summary').innerHTML =
      '<span class="stat"><b>Total Deals:</b> ' + total + '</span>' +
      '<span class="stat"><b>Platform:</b> ' + platforms + '</span>' +
      '<span class="stat"><b>Add-On:</b> ' + addons + '</span>' +
      '<span class="stat"><b>Other:</b> ' + others + '</span>' +
      '<span class="stat"><b>In Target Markets:</b> ' + tm + '</span>';

    var rows = filteredComps();
    var tbody = document.getElementById('comps-tbody');
    tbody.innerHTML = rows.map(function (d, i) { return renderRow(d, i); }).join('');
    document.getElementById('row-count').textContent = 'Showing ' + rows.length + ' of ' + total + ' transactions';

    tbody.querySelectorAll('tr.row-clickable').forEach(function (tr) {
      tr.addEventListener('click', function () { toggleRow(parseInt(tr.dataset.idx, 10)); });
    });
  }

  function renderRow(d, i) {
    var tmCls = d.is_target_market ? 'row-target-market' : '';
    var dot = d.is_target_market ? '🎯 ' : '';
    var dateCell = escapeHtml(d.date || '');
    if (d.date_confidence === 'approximate') {
      dateCell = '<span style="font-style:italic; color:#888;" title="Approximate date — not confirmed against article body">' + dateCell + ' ~</span>';
    }
    var html = '<tr class="row-clickable ' + tmCls + '" data-idx="' + i + '">' +
      '<td>' + dateCell + '</td>' +
      '<td><b>' + dot + escapeHtml(d.target || '') + '</b></td>' +
      '<td>' + escapeHtml(d.acquirer || '') + '</td>' +
      '<td>' + escapeHtml(d.sponsor || '—') + '</td>' +
      '<td>' + escapeHtml(d.location || '') + '</td>' +
      '<td>' + escapeHtml(d.deal_type || '') + '</td>' +
      '<td>' + escapeHtml(d.deal_size || '') + '</td>' +
      '<td>' + escapeHtml(d.multiple || 'N/A') + '</td>' +
      '<td>' + escapeHtml(d.services || '') + '</td>' +
      '</tr>';
    if (compsState.expandedRows.has(i)) {
      var summaryHtml = '';
      var summary = d.deal_summary;
      if (Array.isArray(summary) && summary.length) {
        summaryHtml = '<div style="margin-top:2px;"><b>Deal summary:</b><ul style="margin:4px 0 4px 18px; padding:0;">' +
          summary.map(function (s) { return '<li style="margin-bottom:3px;">' + escapeHtml(s) + '</li>'; }).join('') +
          '</ul></div>';
      } else if (typeof summary === 'string' && summary.trim()) {
        summaryHtml = '<div style="margin-top:2px;"><b>Deal summary:</b> ' + escapeHtml(summary) + '</div>';
      }
      var sourceHtml = '';
      if (d.source_url) {
        sourceHtml = '<div style="margin-top:6px;"><b>Source:</b> <a href="' + escapeHtml(d.source_url) + '" target="_blank" rel="noopener">' + escapeHtml(d.source || 'link') + ' ↗</a></div>';
      } else if (d.source) {
        sourceHtml = '<div style="margin-top:6px;"><b>Source:</b> ' + escapeHtml(d.source) +
          ' <span style="color:#b03030; font-size:11px;">(URL not verified — original press release / blog post needs to be located)</span></div>';
      }
      var findBtn = '';
      if (typeof d.latitude === 'number' && typeof d.longitude === 'number') {
        // Use data-* attributes (HTML-escaped) instead of an inline
        // onclick — JSON.stringify emits unescaped double quotes that
        // collide with the attribute's own double-quote wrapper.
        findBtn = '<button class="find-on-map-btn"' +
          ' data-lat="' + d.latitude + '"' +
          ' data-lng="' + d.longitude + '"' +
          ' data-target="' + escapeHtml(d.target || '') + '"' +
          ' data-acquirer="' + escapeHtml(d.acquirer || '') + '"' +
          ' data-date="' + escapeHtml(d.date || '') + '"' +
          ' data-zoom="' + escapeHtml(d.zoom_hint || 'narrow') + '"' +
          '>🗺️ Find on Map</button>';
      }
      html += '<tr class="expanded-row"><td colspan="9" class="detail-cell">' +
        summaryHtml +
        sourceHtml +
        (d.owner_classification ? '<div style="margin-top:4px;"><b>Owner type:</b> ' + escapeHtml(d.owner_classification) + '</div>' : '') +
        (d.notes ? '<div style="margin-top:6px;"><b>Notes:</b> ' + escapeHtml(d.notes) + '</div>' : '') +
        (d.is_target_market ? '<div style="margin-top:6px; color:#27ae60;"><b>Target market:</b> ' + escapeHtml(d.target_market_name || 'flagged') + '</div>' : '') +
        findBtn +
        '</td></tr>';
    }
    return html;
  }

  function toggleRow(i) {
    if (compsState.expandedRows.has(i)) compsState.expandedRows.delete(i);
    else compsState.expandedRows.add(i);
    renderComps();
  }

  function setSort(k) {
    if (compsState.sortKey === k) {
      compsState.sortDir = compsState.sortDir === 'asc' ? 'desc' : 'asc';
    } else {
      compsState.sortKey = k;
      compsState.sortDir = 'desc';
    }
    renderComps();
  }

  function csvCell(v) {
    if (v === null || v === undefined) return '';
    if (Array.isArray(v)) v = v.join(' | ');
    var s = String(v);
    if (/[",\n]/.test(s)) return '"' + s.replace(/"/g, '""') + '"';
    return s;
  }
  function exportCsv() {
    var rows = filteredComps();
    var cols = ['date','target','acquirer','sponsor','location','deal_type','deal_size','multiple','services','owner_classification','source','source_url','is_target_market','target_market_name','deal_summary','notes'];
    var csv = [cols.join(',')].concat(rows.map(function (r) {
      return cols.map(function (c) { return csvCell(r[c]); }).join(',');
    })).join('\n');
    var blob = new Blob([csv], { type: 'text/csv' });
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url; a.download = 'fog_comps_' + (new Date()).toISOString().slice(0,10) + '.csv';
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }

  // populate year dropdown from data
  (function () {
    var data = (typeof compDatabaseData !== 'undefined') ? compDatabaseData : [];
    var years = {};
    data.forEach(function (d) { var y = compYear(d); if (y) years[y] = true; });
    var yearList = Object.keys(years).sort().reverse();
    var sel = document.getElementById('comp-year');
    var earlierOpt = sel.querySelector('option[value="Earlier"]');
    yearList.forEach(function (y) {
      if (parseInt(y, 10) < 2020) return;
      var o = document.createElement('option');
      o.value = y; o.textContent = y;
      sel.insertBefore(o, earlierOpt);
    });
  })();

  document.getElementById('comp-platform').addEventListener('change', function (e) { compsState.platform = e.target.value; renderComps(); });
  document.getElementById('comp-deal-type').addEventListener('change', function (e) { compsState.dealType = e.target.value; renderComps(); });
  document.getElementById('comp-year').addEventListener('change', function (e) { compsState.year = e.target.value; renderComps(); });
  document.getElementById('comp-target-only').addEventListener('change', function (e) { compsState.targetOnly = e.target.checked; renderComps(); });
  document.getElementById('comp-search').addEventListener('input', function (e) { compsState.search = e.target.value; renderComps(); });
  document.getElementById('comp-export').addEventListener('click', exportCsv);
  document.querySelectorAll('.comps-table th[data-sort]').forEach(function (th) {
    th.addEventListener('click', function () { setSort(th.dataset.sort); });
  });

  // initial renders
  renderNews();
  renderComps();
})();
</script>

</body>
</html>
"""


def main() -> int:
    if not os.path.exists(SRC_MAP):
        sys.stderr.write(
            f"ERROR: source map HTML not found at {SRC_MAP}\n"
            f"Set FOG_MAP_HTML env var to the path of your existing fog_facility_map.html.\n"
        )
        return 2
    if not os.path.exists(NEWS_JSON) or not os.path.exists(COMPS_JSON):
        sys.stderr.write(f"ERROR: missing seed data files in {os.path.dirname(NEWS_JSON)}\n")
        return 2

    style_inner, body_inner, scripts = slice_original(SRC_MAP)
    scripts, public_counts, filter_counts, kept_records = patch_facility_data(scripts)
    scripts = patch_category_info(scripts)
    body_inner = patch_legend(body_inner)
    body_inner = patch_filter_panel(body_inner)
    scripts, collection_counts = inject_collection_layer(scripts)
    scripts = inject_service_hq(scripts)
    scripts = inject_entity_type_toggles(scripts)
    scripts = inject_map_handle(scripts)

    with open(NEWS_JSON, encoding="utf-8") as f:
        news = json.load(f)
    with open(COMPS_JSON, encoding="utf-8") as f:
        comps = json.load(f)

    metadata = {
        "lastRefreshed": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "newsCount": len(news),
        "compCount": len(comps),
    }

    out = (TEMPLATE
           .replace("__ORIGINAL_STYLE__", style_inner)
           .replace("__BODY_MAP_INNER__", body_inner)
           .replace("__MAP_SCRIPTS__", scripts)
           .replace("__NEWS_FEED_JSON__", json.dumps(news, indent=2))
           .replace("__COMP_DATABASE_JSON__", json.dumps(comps, indent=2))
           .replace("__METADATA_JSON__", json.dumps(metadata, indent=2)))

    os.makedirs(os.path.dirname(DST), exist_ok=True)
    with open(DST, "w", encoding="utf-8") as f:
        f.write(out)
    print(f"Wrote {DST} ({os.path.getsize(DST):,} bytes) — "
          f"{len(news)} news items, {len(comps)} deals")

    # ---- Filter summary ----
    before = filter_counts.get("_before", 0)
    after = filter_counts.get("_after", 0)
    if before:
        print()
        print(f"FOG-only filter: BEFORE {before:,} → AFTER {after:,} "
              f"(removed {before - after:,}, {(before - after) / before:.0%})")
        print("  Removed by rule:")
        print(f"    {filter_counts.get('removed_potw', 0):>5}  "
              "POTW name pattern (already covered by WWTP layer)")
        print(f"    {filter_counts.get('removed_solid_waste_company', 0):>5}  "
              "solid-waste-company owner without core-FOG term in name")
        print(f"    {filter_counts.get('removed_negative_keyword', 0):>5}  "
              "negative-keyword name (landfill / recycling / hazardous / etc.)")
        print(f"    {filter_counts.get('removed_no_fog_signal', 0):>5}  "
              "ambiguous NAICS / no FOG-positive keyword in name")
        print("  Kept by rule:")
        print(f"    {filter_counts.get('kept_naics_core', 0):>5}  "
              "NAICS 562991 (septic) or 311613 (rendering) — kept regardless")
        print(f"    {filter_counts.get('kept_ambig_naics_with_keyword', 0):>5}  "
              "NAICS 562219 / 562111 / 221320 with FOG-positive keyword")
        print(f"    {filter_counts.get('kept_keyword_only', 0):>5}  "
              "no relevant NAICS but FOG-positive keyword in name")

    # ---- Per-category remaining ----
    cat_counts: dict[str, int] = {}
    for r in kept_records:
        cat_counts[r.get("ct", "?")] = cat_counts.get(r.get("ct", "?"), 0) + 1
    cat_label = {
        "LES": "LES (Goldman Sachs)",
        "WRE": "Wind River (Gryphon)",
        "BAK": "Baker Commodities",
        "DAR": "Darling / DAR PRO",
        "PUB": "Public Company",
        "PE":  "PE-Backed (Other)",
        "REG": "Regional Operator",
        "LOC": "Local / Family",
        "MUN": "Municipal (flagged)",
        "UNK": "Unknown",
    }
    print()
    print("Remaining by owner_type:")
    for ct in ["LES", "WRE", "BAK", "DAR", "PUB", "PE", "REG", "LOC", "MUN", "UNK"]:
        n = cat_counts.get(ct, 0)
        if n:
            print(f"  {n:>5}  {cat_label.get(ct, ct)}")

    # ---- Public Company breakdown (Darling vs other) ----
    pub_counts = {k: v for k, v in public_counts.items() if k.startswith("Public:")}
    if pub_counts:
        total = sum(pub_counts.values())
        print()
        print(f"Public Company tier ({total} facilities):")
        for label, n in sorted(pub_counts.items(), key=lambda kv: -kv[1]):
            print(f"  {n:>5}  {label}")
        non_darling_pub_records = [
            r for r in kept_records
            if r.get("ct") == "PUB"
            and "darling" not in (r.get("ot") or "").lower()
        ]
        print(f"\nNon-Darling Public Company survivors: {len(non_darling_pub_records)}")
        if 0 < len(non_darling_pub_records) <= 60:
            for r in sorted(non_darling_pub_records,
                            key=lambda x: (x.get("ot") or "", x.get("n") or "")):
                print(f"  {(r.get('ot') or '?')[:55]:55} | "
                      f"{(r.get('n') or '?')[:60]} ({r.get('s') or '?'})")
        elif len(non_darling_pub_records) > 60:
            print("  (>60 — listing first 30 by owner)")
            for r in sorted(non_darling_pub_records,
                            key=lambda x: (x.get("ot") or "", x.get("n") or ""))[:30]:
                print(f"  {(r.get('ot') or '?')[:55]:55} | "
                      f"{(r.get('n') or '?')[:60]} ({r.get('s') or '?'})")

    reg_counts = {k: v for k, v in public_counts.items() if k.startswith("Regional:")}
    if reg_counts:
        print()
        print("Regional brand reclassifications:")
        for label, n in sorted(reg_counts.items(), key=lambda kv: -kv[1]):
            print(f"  {n:>5}  {label}")

    if collection_counts:
        print()
        print("=" * 60)
        print("FACILITY MAP SUMMARY")
        print("=" * 60)
        print(f"Processing plants:    {len(kept_records):>6,}  (FOG-filtered, plants only)")
        total_coll = sum(collection_counts.values())
        print(f"Collection operators: {total_coll:>6,}  (NAICS 562991 + collection-keyword names)")
        print(f"Total map entities:   {len(kept_records) + total_coll:>6,}  "
              f"(+ ~14k WWTPs in separate layer)")
        print()
        print("Collection operators by owner_type:")
        for k, v in sorted(collection_counts.items(), key=lambda kv: -kv[1]):
            print(f"  {v:>6,}  {_BUCKET_LABEL.get(k, k)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
