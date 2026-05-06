#!/usr/bin/env python3
"""Recover consolidator facilities the upstream FRS pre-filter dropped.

The upstream pipeline (filter_facilities.py in fog_map_project) keeps
FRS rows by NAICS-set or FOG name-keyword. Many real consolidator
subsidiaries have generic names (e.g. "Stanley Environmental",
"Brockwell Station") and don't carry one of those NAICS, so they get
dropped — even though the brand match in ownership.py would have
classified them correctly.

This script does a focused supplemental pass: stream the raw 2.6 GB
NATIONAL_SINGLE.CSV, find rows whose name matches a known consolidator
brand, dedupe against whatever's already in docs/index.html FOG_DATA
(by REGISTRY_ID), classify entity_type and brand bucket, and write
data/consolidator_supplements.json. build_index.py merges this into
FOG_DATA before applying its own filter.

Usage (one-shot, uses the fog_map_project venv with pandas + numpy):
    /Users/naeemmuscatwalla/fog_map_project/venv/bin/python \\
        scripts/extract_consolidator_supplements.py
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import time

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRS_CSV = "/Users/naeemmuscatwalla/fog_map_project/NATIONAL_SINGLE.CSV"
HTML_PATH = os.path.join(ROOT, "docs", "index.html")
OUT_JSON = os.path.join(ROOT, "data", "consolidator_supplements.json")

USE_COLS = [
    "REGISTRY_ID", "PRIMARY_NAME", "LOCATION_ADDRESS", "CITY_NAME",
    "STATE_CODE", "POSTAL_CODE", "LATITUDE83", "LONGITUDE83",
    "NAICS_CODES",
]

# Specific brand patterns — long enough to minimize false positives.
# Each entry is (regex_pattern, ct_code, owner_type_label, owner_parent).
# Use word-boundary regex anchoring instead of bare substring so
# "Wind River Mining" and "Wind River Systems" don't get tagged WRE.
_BRAND_DEFS: list[tuple[str, str, str, str]] = [
    # LES + subsidiaries
    (r"liquid environmental solutions", "LES", "LES (Goldman Sachs)", "Liquid Environmental Solutions"),
    (r"all city environmental",         "LES", "LES (Goldman Sachs)", "Liquid Environmental Solutions"),
    (r"atlas pumping service",          "LES", "LES (Goldman Sachs)", "Liquid Environmental Solutions"),
    (r"value stream environmental",     "LES", "LES (Goldman Sachs)", "Liquid Environmental Solutions"),
    (r"flohawks",                       "LES", "LES (Goldman Sachs)", "Liquid Environmental Solutions"),
    (r"giddings hawkins",               "LES", "LES (Goldman Sachs)", "Liquid Environmental Solutions"),
    (r"new orleans grease trap",        "LES", "LES (Goldman Sachs)", "Liquid Environmental Solutions"),
    (r"grease masters recycl",          "LES", "LES (Goldman Sachs)", "Liquid Environmental Solutions"),
    (r"green arrow environmental",      "LES", "LES (Goldman Sachs)", "Liquid Environmental Solutions"),
    (r"all american grease",            "LES", "LES (Goldman Sachs)", "Liquid Environmental Solutions"),
    (r"affordable bio feedstock",       "LES", "LES (Goldman Sachs)", "Liquid Environmental Solutions"),
    (r"carolinas resource recovery",    "LES", "LES (Goldman Sachs)", "Liquid Environmental Solutions"),
    (r"commercial pumping services",    "LES", "LES (Goldman Sachs)", "Liquid Environmental Solutions"),
    (r"dover grease trap",              "LES", "LES (Goldman Sachs)", "Liquid Environmental Solutions"),
    (r"newstream environmental",        "LES", "LES (Goldman Sachs)", "Liquid Environmental Solutions"),

    # Wind River Environmental + subsidiaries (longer phrases only —
    # "wind river" alone catches mining and software cos)
    (r"wind river environmental",       "WRE", "Wind River (Gryphon)", "Wind River Environmental"),
    (r"seminole septic",                "WRE", "Wind River (Gryphon)", "Wind River Environmental"),
    (r"mid south septic|mid-south septic", "WRE", "Wind River (Gryphon)", "Wind River Environmental"),
    (r"greenway waste solutions",       "WRE", "Wind River (Gryphon)", "Wind River Environmental"),
    (r"tcw wastewater",                 "WRE", "Wind River (Gryphon)", "Wind River Environmental"),
    (r"keystone wastewater services",   "WRE", "Wind River (Gryphon)", "Wind River Environmental"),
    (r"hapchuk",                        "WRE", "Wind River (Gryphon)", "Wind River Environmental"),
    (r"stanley environmental solutions","WRE", "Wind River (Gryphon)", "Wind River Environmental"),
    (r"brownie's septic|brownies septic","WRE", "Wind River (Gryphon)", "Wind River Environmental"),
    (r"a-1 gator wastewater|a1 gator",  "WRE", "Wind River (Gryphon)", "Wind River Environmental"),
    (r"cooke's plumbing|cookes plumbing","WRE", "Wind River (Gryphon)", "Wind River Environmental"),
    (r"metro rooter|metro-rooter",      "WRE", "Wind River (Gryphon)", "Wind River Environmental"),
    (r"felix septic",                   "WRE", "Wind River (Gryphon)", "Wind River Environmental"),
    (r"eastern pipe service",           "WRE", "Wind River (Gryphon)", "Wind River Environmental"),
    (r"john matthes septic",            "WRE", "Wind River (Gryphon)", "Wind River Environmental"),
    (r"triple t pumping",               "WRE", "Wind River (Gryphon)", "Wind River Environmental"),
    (r"koberlein environmental",        "WRE", "Wind River (Gryphon)", "Wind River Environmental"),
    (r"m&s septic|m and s septic",      "WRE", "Wind River (Gryphon)", "Wind River Environmental"),
    (r"fenkner septic",                 "WRE", "Wind River (Gryphon)", "Wind River Environmental"),
    (r"east coast resources",           "WRE", "Wind River (Gryphon)", "Wind River Environmental"),
    (r"b ?\\& ?p environmental|b and p environmental", "WRE", "Wind River (Gryphon)", "Wind River Environmental"),
    (r"liquid assets disposal",         "WRE", "Wind River (Gryphon)", "Wind River Environmental"),
    (r"j ?\\& ?m transfer|j and m transfer", "WRE", "Wind River (Gryphon)", "Wind River Environmental"),
    (r"drummac septic",                 "WRE", "Wind River (Gryphon)", "Wind River Environmental"),
    (r"hartigan wastewater",            "WRE", "Wind River (Gryphon)", "Wind River Environmental"),
    (r"earthcare systems",              "WRE", "Wind River (Gryphon)", "Wind River Environmental"),
    (r"kaiser-?battistone",             "WRE", "Wind River (Gryphon)", "Wind River Environmental"),
    (r"kline's services|klines services","WRE", "Wind River (Gryphon)", "Wind River Environmental"),
    (r"jim leboeuf septic|leboeuf septic","WRE", "Wind River (Gryphon)", "Wind River Environmental"),
    (r"soucy's septic|soucys septic",   "WRE", "Wind River (Gryphon)", "Wind River Environmental"),
    (r"stright sewage disposal",        "WRE", "Wind River (Gryphon)", "Wind River Environmental"),
    (r"oxbury sanitation",              "WRE", "Wind River (Gryphon)", "Wind River Environmental"),
    (r"mahopac septic",                 "WRE", "Wind River (Gryphon)", "Wind River Environmental"),
    (r"hamby's septic|hambys septic|hamby's commercial waste", "WRE", "Wind River (Gryphon)", "Wind River Environmental"),
    (r"gibson septic",                  "WRE", "Wind River (Gryphon)", "Wind River Environmental"),
    (r"select processing of orlando",   "WRE", "Wind River (Gryphon)", "Wind River Environmental"),
    (r"tillman septic",                 "WRE", "Wind River (Gryphon)", "Wind River Environmental"),
    (r"franc environmental",            "WRE", "Wind River (Gryphon)", "Wind River Environmental"),
    (r"kbx golden",                     "WRE", "Wind River (Gryphon)", "Wind River Environmental"),
    (r"affordable pumping services",    "WRE", "Wind River (Gryphon)", "Wind River Environmental"),
    (r"captain clog",                   "WRE", "Wind River (Gryphon)", "Wind River Environmental"),
    (r"dimmick septic",                 "WRE", "Wind River (Gryphon)", "Wind River Environmental"),
    (r"myers septic",                   "WRE", "Wind River (Gryphon)", "Wind River Environmental"),
    (r"skyline plumbing",               "WRE", "Wind River (Gryphon)", "Wind River Environmental"),
    (r"all florida septic",             "WRE", "Wind River (Gryphon)", "Wind River Environmental"),
    (r"drain innovations",              "WRE", "Wind River (Gryphon)", "Wind River Environmental"),
    (r"church view septic",             "WRE", "Wind River (Gryphon)", "Wind River Environmental"),
    (r"1st choice service",             "WRE", "Wind River (Gryphon)", "Wind River Environmental"),
    (r"waste water services inc",       "WRE", "Wind River (Gryphon)", "Wind River Environmental"),
    (r"a sanitary pumping",             "WRE", "Wind River (Gryphon)", "Wind River Environmental"),
    (r"heritage pumping",               "WRE", "Wind River (Gryphon)", "Wind River Environmental"),
    (r"parent sanitation",              "WRE", "Wind River (Gryphon)", "Wind River Environmental"),
    (r"cloud 9 services",               "WRE", "Wind River (Gryphon)", "Wind River Environmental"),
    (r"aa cut rate septic",             "WRE", "Wind River (Gryphon)", "Wind River Environmental"),
    (r"rosey's tank cleaning|roseys tank cleaning", "WRE", "Wind River (Gryphon)", "Wind River Environmental"),
    (r"brockwell['s]? septic",          "WRE", "Wind River (Gryphon)", "Wind River Environmental"),

    # Baker Commodities (require "Inc"/"Commod"/"Rendering" so generic
    # "Baker" doesn't match every Baker-named business)
    (r"baker commodit",                 "BAK", "Baker Commodities", "Baker Commodities"),
    (r"baker rendering",                "BAK", "Baker Commodities", "Baker Commodities"),
    (r"new leaf biofuel",               "BAK", "Baker Commodities", "Baker Commodities"),
    (r"abp recyclers",                  "BAK", "Baker Commodities", "Baker Commodities"),

    # Darling Ingredients + subsidiaries
    (r"darling ingredients",            "DAR", "Darling/DAR PRO", "Darling Ingredients / DAR PRO"),
    (r"darling international",          "DAR", "Darling/DAR PRO", "Darling Ingredients / DAR PRO"),
    (r"\bdar pro\b|\bdar-pro\b",        "DAR", "Darling/DAR PRO", "Darling Ingredients / DAR PRO"),
    (r"valley proteins",                "DAR", "Darling/DAR PRO", "Darling Ingredients / DAR PRO"),
    (r"sanimax",                        "DAR", "Darling/DAR PRO", "Darling Ingredients / DAR PRO"),
    (r"griffin industries",             "DAR", "Darling/DAR PRO", "Darling Ingredients / DAR PRO"),
    (r"diamond green diesel",           "DAR", "Darling/DAR PRO", "Darling Ingredients / DAR PRO"),
    (r"bakery feeds",                   "DAR", "Darling/DAR PRO", "Darling Ingredients / DAR PRO"),
    (r"craig protein",                  "DAR", "Darling/DAR PRO", "Darling Ingredients / DAR PRO"),
    (r"carolina by-?products",          "DAR", "Darling/DAR PRO", "Darling Ingredients / DAR PRO"),

    # Mahoney/Crimson (Neste)
    (r"mahoney environmental",          "MAH", "Mahoney/Crimson",  "Mahoney Environmental"),
    (r"crimson renewable",              "MAH", "Mahoney/Crimson",  "Mahoney Environmental"),
    (r"sequential environmental services", "MAH", "Mahoney/Crimson", "Mahoney Environmental"),

    # Eazy Grease + subsidiaries
    (r"eazy grease",                    "EAZ", "Eazy Grease",       "Eazy Grease"),
    (r"dht grease",                     "EAZ", "Eazy Grease",       "Eazy Grease"),
    (r"daytona biodiesel",              "EAZ", "Eazy Grease",       "Eazy Grease"),
    (r"green nature recycling",         "EAZ", "Eazy Grease",       "Eazy Grease"),

    # Public small-cap
    (r"barrel energy",                  "BAR", "Barrel Energy",     "Barrel Energy"),
    (r"happy traps",                    "BAR", "Barrel Energy",     "Barrel Energy"),

    # Other PE-backed
    (r"momentum environmental",         "MOM", "Momentum Environmental", "Momentum Environmental"),
    (r"septic blue",                    "SEP", "Septic Blue (Georgia Oak)", "Septic Blue"),
    (r"heritage[- ]crystal clean|crystal clean", "PE", "PE-Backed: Heritage-Crystal Clean", "Heritage-Crystal Clean"),
    (r"patriot environmental services", "PE", "PE-Backed: Heritage-Crystal Clean", "Patriot Environmental"),
    (r"\bsynagro\b",                    "PE", "PE-Backed: Synagro Technologies",   "Synagro Technologies"),
    (r"denali water solutions",         "PE", "PE-Backed: Denali Water Solutions", "Denali Water Solutions"),
    (r"\btradebe\b",                    "PE", "PE-Backed: Tradebe Environmental",  "Tradebe Environmental"),
    (r"\bhepaco\b",                     "PE", "PE-Backed: HEPACO (Clean Harbors)", "HEPACO"),
    (r"chuck's septic|chucks septic",   "PE", "PE-Backed: Chuck's Septic / CST",   "Chuck's Septic / CST"),
    (r"cst utilities",                  "PE", "PE-Backed: Chuck's Septic / CST",   "Chuck's Septic / CST"),
    (r"restaurant technologies",        "PE", "PE-Backed: Restaurant Technologies","Restaurant Technologies"),
]

_COMPILED = [(re.compile(r"\b(?:" + p + r")\b", re.IGNORECASE), ct, ot, op)
             for p, ct, ot, op in _BRAND_DEFS]


PLANT_NAICS = {"562219", "311613", "221320"}
PUMPER_NAICS = {"562991"}
PLANT_NAME_KW = ["treatment plant", "treatment facility", "wrf", "wpcp", "wpcf",
                 "rendering", "processing", "disposal", "transfer station",
                 "recycling center", "biofuel", "biodiesel", "biomass", "compost",
                 "incinerator", "energy recovery", "wastewater plant", "treatment center",
                 "reclamation"]
PUMPER_NAME_KW = ["septic", "pumping", "vacuum", "drain cleaning", "porta-potty",
                  "porta potty", "portable toilet", "portable restroom", "honey",
                  "rooter", "drains"]


def _classify_entity(name: str, naics: set[str]) -> str:
    n = (name or "").lower()
    if naics & PLANT_NAICS or any(kw in n for kw in PLANT_NAME_KW):
        return "plant"
    if naics & PUMPER_NAICS or any(kw in n for kw in PUMPER_NAME_KW):
        return "pumper"
    return "plant"


def _match_brand(name: str) -> tuple[str, str, str] | None:
    for rx, ct, ot, op in _COMPILED:
        if rx.search(name):
            return ct, ot, op
    return None


def load_existing_ids() -> set[str]:
    if not os.path.exists(HTML_PATH):
        return set()
    with open(HTML_PATH, encoding="utf-8") as f:
        h = f.read()
    m = re.search(r"const FOG_DATA = (\[.*?\]);\s*\n", h, re.DOTALL)
    if not m:
        return set()
    return {r.get("i", "") for r in json.loads(m.group(1))}


def main() -> int:
    if not os.path.exists(FRS_CSV):
        sys.stderr.write(f"Missing {FRS_CSV}\n"); return 2

    existing = load_existing_ids()
    print(f"Existing FOG_DATA registry IDs: {len(existing):,}")

    chunk_size = 200_000
    rows_seen = 0
    kept: list[dict] = []
    t0 = time.time()
    print("Streaming FRS...")
    reader = pd.read_csv(FRS_CSV, usecols=USE_COLS, chunksize=chunk_size,
                        dtype=str, encoding="latin-1", on_bad_lines="skip",
                        low_memory=False)
    for ci, chunk in enumerate(reader):
        rows_seen += len(chunk)
        chunk = chunk.dropna(subset=["PRIMARY_NAME"])
        for _, row in chunk.iterrows():
            rid = str(row.get("REGISTRY_ID") or "")
            if not rid or rid in existing:
                continue
            name = str(row.get("PRIMARY_NAME") or "")
            match = _match_brand(name)
            if match is None:
                continue
            try:
                lat = float(row.get("LATITUDE83") or "nan")
                lng = float(row.get("LONGITUDE83") or "nan")
            except (TypeError, ValueError):
                continue
            if not (17 < lat < 72) or not (-180 < lng < -64):
                continue
            ct, ot, op = match
            naics_str = str(row.get("NAICS_CODES") or "")
            naics = {p.strip() for p in re.split(r"[;,]", naics_str) if p.strip()}
            entity = _classify_entity(name, naics)
            kept.append({
                "i": rid,
                "n": name,
                "ad": str(row.get("LOCATION_ADDRESS") or ""),
                "c": str(row.get("CITY_NAME") or ""),
                "s": str(row.get("STATE_CODE") or ""),
                "z": str(row.get("POSTAL_CODE") or ""),
                "la": round(lat, 5),
                "lo": round(lng, 5),
                "na": naics_str,
                "op": op,
                "ot": ot,
                "ct": ct,
                "e": entity,
                "nw": "", "nd": None, "ncs": "",  # nearest WWTP not computed for supplements
                "rda": False, "dn": "", "dd": "", "dc": "",
                "_supplemental": True,
            })
        if (ci + 1) % 5 == 0:
            print(f"  chunk {ci+1}: rows={rows_seen:,} kept={len(kept):,} t={time.time()-t0:.0f}s")

    os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(kept, f, separators=(",", ":"))
    print(f"\nWrote {OUT_JSON}: {len(kept):,} supplemental records")

    # Per-ct breakdown
    from collections import Counter
    ct_counts = Counter(r["ct"] for r in kept)
    ent_counts = Counter((r["ct"], r["e"]) for r in kept)
    print()
    print("Recovered consolidator records by ct + entity:")
    for ct in ["LES","WRE","BAK","DAR","MAH","MOM","SEP","BAR","EAZ","PE"]:
        plant = ent_counts.get((ct, "plant"), 0)
        pumper = ent_counts.get((ct, "pumper"), 0)
        if plant + pumper:
            print(f"  {ct}: plant={plant}, pumper={pumper}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
