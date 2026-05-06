#!/usr/bin/env python3
"""Extract collection / service operators from the EPA FRS national CSV.

This is a one-shot local job — it streams the 2.6 GB
NATIONAL_SINGLE.CSV in chunks and produces
data/collection_operators.json. After running this once, scripts/
build_index.py picks up that JSON and embeds it as a separate map
layer alongside the (smaller) processing-plant layer.

Usage (uses the existing fog_map_project venv that already has pandas
and numpy):
    /Users/naeemmuscatwalla/fog_map_project/venv/bin/python \
        scripts/extract_collection_operators.py
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import time
from typing import Iterable

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRS_CSV = "/Users/naeemmuscatwalla/fog_map_project/NATIONAL_SINGLE.CSV"
HTML_PATH = os.path.join(ROOT, "docs", "index.html")
OUT_JSON = os.path.join(ROOT, "data", "collection_operators.json")

USE_COLS = [
    "REGISTRY_ID", "PRIMARY_NAME", "LOCATION_ADDRESS", "CITY_NAME",
    "STATE_CODE", "POSTAL_CODE", "LATITUDE83", "LONGITUDE83",
    "NAICS_CODES",
]


# ============================================================================
# Filter rules — collection / service operators specifically
# ============================================================================

# NAICS 562991 = Septic Tank and Related Services. Anything in this code is
# almost always a collection operator. Keep regardless of name.
PUMPER_NAICS = {"562991"}

# Service-oriented name keywords that strongly imply collection / hauling
# (vs. processing / disposal). Listed in the spec.
COLLECTION_NAME_KEYWORDS = [k.lower() for k in [
    "grease trap clean", "grease trap service", "grease trap pump",
    "grease cleaning", "grease service", "grease pumping",
    "trap cleaning", "trap service", "trap pumping",
    "septic service", "septic pump", "septic tank", "septic clean",
    "drain cleaning", "drain service", "rooter",
    "sewer cleaning", "sewer service",
    "liquid waste service", "liquid waste haul", "liquid waste transport",
    "vacuum truck", "vac truck", "vac service",
    "portable toilet", "portable sanitation", "porta potty", "porta john",
    "grease removal", "grease collection", "grease hauling",
    "fog service", "fog cleaning", "fog removal",
    "cooking oil collection", "cooking oil pickup", "cooking oil recycl",
    "uco collection", "used oil collection",
    "hydro jetting", "hydro vac", "hydroexcavation",
    "catch basin clean", "storm drain clean",
    "pumping service", "pump out service",
]]

# Keywords that, when matched, only count as a collection operator if
# COMBINED with at least one strong keyword from COLLECTION_NAME_KEYWORDS.
# (e.g., "Plumbing" alone is too generic; "ABC Plumbing & Septic" is fine.)
WEAK_KEYWORDS_REQUIRES_STRONG = [k.lower() for k in [
    "plumbing",
]]
STRONG_KEYWORDS_FOR_WEAK = [k.lower() for k in [
    "grease", "septic", "drain", "sewer", "fog", "vacuum",
]]


# Same negative / municipal filters as the build-time filter applies to
# processing plants. Keeping these aligned avoids contradictory survivors
# between the two layers.
NEGATIVE_KEYWORDS = [k.lower() for k in [
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
    # Hazardous / medical
    "hazardous waste", "RCRA", "PCB", "radioactive", "nuclear",
    "chemical waste", "toxic",
    "medical waste", "biohazard", "pharmaceutical", "sharps",
    "pathological", "infectious waste", "stericycle",
    # Other
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

POTW_PATTERNS = [k.lower() for k in [
    "wwtp", "wwtf", "wpcp", "wpcf", "potw",
    "wastewater treatment", "wastewater reclamation",
    "wastewater system", "wastewater utility",
    "water treatment plant", "water treatment facility",
    "water reclamation", "sewage treatment",
    "sewage lagoon", "sewer lagoon", "wastewater lagoon",
    "waste water treatment", "treatment lagoon",
    "water authority", "water district", "water utility",
    "sewer district", "sewer authority", "sanitation district",
]]

MUNICIPAL_PATTERNS = [
    r"\bcity of\b", r"\bcounty of\b", r"\btown of\b", r"\bvillage of\b",
    r"\bborough of\b", r"\btownship of\b",
    r"\bmunicipal\b", r"\bpotw\b", r"\bsewerage authority\b",
    r"\butility district\b", r"\bsanitary district\b",
]
MUNICIPAL_RX = re.compile("|".join(MUNICIPAL_PATTERNS), re.IGNORECASE)


def _naics_set(s: str) -> set[str]:
    if not isinstance(s, str) or not s:
        return set()
    return {c.strip() for c in s.split(";") if c.strip()}


def _has_collection_keyword(name_low: str) -> bool:
    """True if name contains a collection-operator keyword. Handles the
    'Plumbing-only-with-strong' rule from the spec."""
    if any(kw in name_low for kw in COLLECTION_NAME_KEYWORDS):
        return True
    if any(kw in name_low for kw in WEAK_KEYWORDS_REQUIRES_STRONG):
        if any(kw in name_low for kw in STRONG_KEYWORDS_FOR_WEAK):
            return True
    return False


def _is_municipal(name: str) -> bool:
    return bool(MUNICIPAL_RX.search(name or ""))


def _is_potw(name_low: str) -> bool:
    return any(p in name_low for p in POTW_PATTERNS)


def _has_negative_keyword(name_low: str) -> bool:
    return any(kw in name_low for kw in NEGATIVE_KEYWORDS)


def passes_collection_filter(row) -> bool:
    name = row.get("PRIMARY_NAME") or ""
    if not isinstance(name, str) or not name.strip():
        return False
    name_low = name.lower()
    naics = _naics_set(row.get("NAICS_CODES") or "")

    # Must match collection criteria
    naics_match = bool(naics & PUMPER_NAICS)
    keyword_match = _has_collection_keyword(name_low)
    if not (naics_match or keyword_match):
        return False

    # Hard exclusions
    if _is_municipal(name):
        return False
    if _is_potw(name_low):
        return False
    if _has_negative_keyword(name_low):
        return False

    return True


# ============================================================================
# Brand matching (ported from ~/fog_map_project/ownership.py — kept here so
# this script doesn't take a runtime dep on the upstream project)
# ============================================================================

LES_BRANDS = [
    "liquid environmental solutions",
    "advance plumbing", "advance environmental services",
    "all city environmental", "atlas pumping service",
    "carolinas resource recovery", "ciro's sewer cleaning", "ciros sewer cleaning",
    "commercial pumping services", "dal-worth industries", "dalworth industries",
    "dover grease trap", "dover environmental", "flohawks", "giddings hawkins",
    "gordon's american waste", "gordons american waste", "grease masters",
    "green arrow environmental", "lyles grease trap", "new orleans grease trap",
    "newstream", "rite-way industrial", "rite way industrial",
    "value stream environmental", "affordable bio feedstock",
    "all american grease services", "a-1 waste management",
]
WRE_BRANDS = [
    "wind river environmental", "seminole septic", "mid south septic",
    "mid-south septic", "greenway waste solutions", "tcw wastewater",
    "brockwell", "hapchuk", "keystone wastewater", "stanley environmental",
    "brownie's septic", "brownies septic", "a1 gator", "a-1 gator",
    "cooke's plumbing", "cookes plumbing", "metro rooter", "metro-rooter",
    "felix septic", "eastern pipe service", "john matthes septic",
    "triple t pumping", "koberlein environmental", "m&s septic",
    "m & s septic", "m and s septic", "fenkner septic", "drummac septic",
    "hartigan wastewater", "earthcare", "kaiser-battistone",
    "kaiser battistone", "kline's services", "klines services",
    "jim leboeuf septic", "leboeuf septic", "soucy's septic", "soucys septic",
    "stright sewage disposal", "oxbury sanitation", "mahopac septic",
    "hamby's septic", "hambys septic", "hamby's commercial waste",
    "gibson septic", "select processing of orlando", "tillman septic pumping",
    "franc environmental", "kbx golden", "affordable pumping services",
    "captain clog drain", "dimmick septic", "myers septic",
    "skyline plumbing & septic", "all florida septic", "drain innovations",
    "church view septic", "1st choice service", "b & p environmental",
    "b and p environmental", "waste water services inc", "a sanitary pumping",
    "heritage pumping", "parent sanitation", "cloud 9 services",
    "aa cut rate septic", "east coast resources", "liquid assets disposal",
    "j&m transfer", "j and m transfer", "earth farms organics",
    "rosey's tank cleaning", "roseys tank cleaning",
]
BAKER_BRANDS = [
    "baker commodities", "baker rendering", "baker grease",
    "new leaf biofuel", "western mass rendering",
    "american by-products", "american by products", "abp recyclers",
]
DARLING_BRANDS = [
    "darling ingredients", "darling international", "dar pro", "dar-pro",
    "valley proteins", "sanimax", "griffin industries", "rothsay",
    "craig protein", "carolina by-products", "carolina by products",
    "bakery feeds", "enviroflight", "diamond green diesel", "triple t foods",
]
EAZY_BRANDS = [
    "eazy grease", "dht grease solutions", "relentless renewables",
    "daytona biodiesel", "cleanfri", "liquid recovery solutions",
    "green nature recycling",
]
MOMENTUM_BRANDS = ["momentum environmental"]
SEPTIC_BLUE_BRANDS = ["septic blue"]
BARREL_BRANDS = ["barrel energy", "happy traps"]


def _compile_brands(brands: list[str]) -> list[re.Pattern]:
    return [re.compile(r"\b" + re.escape(b) + r"\b", re.IGNORECASE) for b in brands]


_LES_RX = _compile_brands(LES_BRANDS)
_WRE_RX = _compile_brands(WRE_BRANDS)
_BAK_RX = _compile_brands(BAKER_BRANDS)
_DAR_RX = _compile_brands(DARLING_BRANDS)
_EAZY_RX = _compile_brands(EAZY_BRANDS)
_MOM_RX = _compile_brands(MOMENTUM_BRANDS)
_SEP_RX = _compile_brands(SEPTIC_BLUE_BRANDS)
_BAR_RX = _compile_brands(BARREL_BRANDS)


def match_brand(name: str) -> tuple[str, str]:
    """Return (owner_parent, owner_type) — the second is the cluster bucket
    used by the map ('LES (Goldman Sachs)', 'Wind River (Gryphon)',
    'Public: Darling Ingredients (NYSE: DAR)', 'Independent', etc.)."""
    n = name or ""
    for rx in _WRE_RX:
        if rx.search(n):
            return ("Wind River Environmental", "Wind River (Gryphon)")
    for rx in _LES_RX:
        if rx.search(n):
            return ("Liquid Environmental Solutions", "LES (Goldman Sachs)")
    for rx in _DAR_RX:
        if rx.search(n):
            return ("Darling Ingredients / DAR PRO", "Public: Darling Ingredients (NYSE: DAR)")
    for rx in _BAK_RX:
        if rx.search(n):
            return ("Baker Commodities", "Baker Commodities")
    for rx in _EAZY_RX:
        if rx.search(n):
            return ("Eazy Grease", "Eazy Grease")
    for rx in _MOM_RX:
        if rx.search(n):
            return ("Momentum Environmental", "Momentum Environmental")
    for rx in _SEP_RX:
        if rx.search(n):
            return ("Septic Blue", "Septic Blue (Georgia Oak)")
    for rx in _BAR_RX:
        if rx.search(n):
            return ("Barrel Energy", "Public: Barrel Energy (OTC: BRLL)")
    return (n, "Independent")


# ============================================================================
# Coord helpers + nearest-plant computation
# ============================================================================


def _haversine_miles_one_to_many(lat: float, lng: float,
                                  arr_lat: np.ndarray, arr_lng: np.ndarray) -> np.ndarray:
    R = 3958.8
    lat1, lng1 = math.radians(lat), math.radians(lng)
    lat2, lng2 = np.radians(arr_lat), np.radians(arr_lng)
    dlat = lat2 - lat1
    dlng = lng2 - lng1
    a = np.sin(dlat / 2) ** 2 + math.cos(lat1) * np.cos(lat2) * np.sin(dlng / 2) ** 2
    return 2 * R * np.arcsin(np.sqrt(a))


def load_existing_plant_data() -> tuple[set[str], list[dict], list[dict]]:
    """Pull the surviving FOG_DATA from docs/index.html.

    Returns (plant_ids, plants, pumpers) — plant_ids is used for the
    "exclude anything already in the processing plant dataset" dedupe
    step (only PLANTS qualify, since pumpers are themselves collection
    operators and belong in the collection layer). plants is used for
    nearest-plant proximity. pumpers becomes the existing-data baseline
    for the collection layer (the 2,812 pumpers FOG_DATA already had
    classified by entity_type)."""
    if not os.path.exists(HTML_PATH):
        return set(), [], []
    with open(HTML_PATH, encoding="utf-8") as f:
        html = f.read()
    m = re.search(r"const FOG_DATA = (\[.*?\]);\s*\n", html, re.DOTALL)
    if not m:
        return set(), [], []
    records = json.loads(m.group(1))
    plants = [r for r in records if r.get("e") == "plant"
              and r.get("la") is not None and r.get("lo") is not None]
    pumpers = [r for r in records if r.get("e") == "pumper"
               and r.get("la") is not None and r.get("lo") is not None]
    plant_ids = {r.get("i") for r in plants if r.get("i")}
    return plant_ids, plants, pumpers


# ============================================================================
# Main extraction
# ============================================================================


def main() -> int:
    if not os.path.exists(FRS_CSV):
        sys.stderr.write(f"FRS CSV not found at {FRS_CSV}\n")
        return 2

    plant_ids, plants, existing_pumpers = load_existing_plant_data()
    print(f"Existing FOG_DATA: {len(plant_ids):,} plant ids "
          f"({len(plants):,} plants for proximity), {len(existing_pumpers):,} pumpers "
          f"to fold into the collection layer")

    chunk_size = 200_000
    rows_seen = 0
    kept_chunks: list[pd.DataFrame] = []
    t0 = time.time()
    print(f"Streaming {FRS_CSV} (chunk_size={chunk_size:,})...")
    reader = pd.read_csv(
        FRS_CSV, usecols=USE_COLS, chunksize=chunk_size,
        dtype=str, encoding="latin-1", on_bad_lines="skip", low_memory=False,
    )
    for ci, chunk in enumerate(reader):
        rows_seen += len(chunk)
        mask = chunk.apply(passes_collection_filter, axis=1)
        kept = chunk[mask]
        if len(kept):
            kept_chunks.append(kept)
        elapsed = time.time() - t0
        running = sum(len(x) for x in kept_chunks)
        if (ci + 1) % 10 == 0 or rows_seen >= 4_000_000:
            print(f"  chunk {ci+1}: rows seen={rows_seen:,}, kept={running:,}, t={elapsed:.1f}s")
    df = pd.concat(kept_chunks, ignore_index=True) if kept_chunks else pd.DataFrame()
    print(f"After collection-criteria filter: {len(df):,}")

    # Coord cleanup + plausibility check
    df["LATITUDE83"] = pd.to_numeric(df["LATITUDE83"], errors="coerce")
    df["LONGITUDE83"] = pd.to_numeric(df["LONGITUDE83"], errors="coerce")
    df = df.dropna(subset=["LATITUDE83", "LONGITUDE83"])
    df = df[(df["LATITUDE83"] != 0) & (df["LONGITUDE83"] != 0)]
    df = df[(df["LATITUDE83"] > 17) & (df["LATITUDE83"] < 72)]
    df = df[(df["LONGITUDE83"] > -180) & (df["LONGITUDE83"] < -64)]
    df = df.drop_duplicates(subset=["REGISTRY_ID"])
    print(f"After coord validation + dedupe: {len(df):,}")

    # Cross-reference: drop anything already classified as a PROCESSING
    # PLANT in FOG_DATA. Pumpers there are themselves collection
    # operators — fold them into the collection layer below instead.
    before = len(df)
    df = df[~df["REGISTRY_ID"].isin(plant_ids)]
    print(f"After removing facilities already in plants layer: {len(df):,} "
          f"(dropped {before - len(df):,})")

    # Brand matching
    df["_match"] = df["PRIMARY_NAME"].apply(match_brand)
    df["owner_parent"] = df["_match"].apply(lambda t: t[0])
    df["owner_type"] = df["_match"].apply(lambda t: t[1])
    df = df.drop(columns=["_match"])

    # Nearest-plant distance (vectorized in numpy)
    if plants:
        plant_lats = np.array([p["la"] for p in plants])
        plant_lons = np.array([p["lo"] for p in plants])
        plant_names = [p.get("n", "") for p in plants]
        plant_owners = [p.get("op", "") for p in plants]
        nearest_names: list[str] = []
        nearest_dists: list[float] = []
        nearest_owners: list[str] = []
        lats = df["LATITUDE83"].to_numpy()
        lons = df["LONGITUDE83"].to_numpy()
        for i in range(len(df)):
            d = _haversine_miles_one_to_many(lats[i], lons[i], plant_lats, plant_lons)
            j = int(np.argmin(d))
            nearest_names.append(plant_names[j])
            nearest_dists.append(round(float(d[j]), 1))
            nearest_owners.append(plant_owners[j])
        df["nearest_plant_name"] = nearest_names
        df["nearest_plant_distance_miles"] = nearest_dists
        df["nearest_plant_owner"] = nearest_owners
    else:
        df["nearest_plant_name"] = ""
        df["nearest_plant_distance_miles"] = None
        df["nearest_plant_owner"] = ""

    # Materialize as compact JSON with minimal field names so the embedded
    # dataset stays compact in docs/index.html.
    out_records: list[dict] = []
    for r in df.itertuples(index=False):
        rec = {
            "i": str(r.REGISTRY_ID) if not pd.isna(r.REGISTRY_ID) else "",
            "n": str(r.PRIMARY_NAME) if not pd.isna(r.PRIMARY_NAME) else "",
            "ad": str(r.LOCATION_ADDRESS) if not pd.isna(r.LOCATION_ADDRESS) else "",
            "c": str(r.CITY_NAME) if not pd.isna(r.CITY_NAME) else "",
            "s": str(r.STATE_CODE) if not pd.isna(r.STATE_CODE) else "",
            "z": str(r.POSTAL_CODE) if not pd.isna(r.POSTAL_CODE) else "",
            "la": round(float(r.LATITUDE83), 5),
            "lo": round(float(r.LONGITUDE83), 5),
            "na": str(r.NAICS_CODES) if not pd.isna(r.NAICS_CODES) else "",
            "op": r.owner_parent,
            "o": r.owner_type,
            "np": r.nearest_plant_name,
            "nd": r.nearest_plant_distance_miles,
            "nplo": r.nearest_plant_owner,
            "src": "EPA_FRS",
        }
        # Drop empty optional fields to save bytes.
        for k in ("ad", "z", "na", "np", "nplo"):
            if rec[k] in ("", None):
                rec.pop(k)
        out_records.append(rec)

    # Fold existing FOG_DATA pumpers into the collection layer. They're
    # already brand-matched and have ot/ct from the upstream pipeline;
    # remap to the collection schema and compute nearest plant.
    if existing_pumpers and plants:
        plant_lats = np.array([p["la"] for p in plants])
        plant_lons = np.array([p["lo"] for p in plants])
        plant_names = [p.get("n", "") for p in plants]
        plant_owners = [p.get("op", "") for p in plants]
        for p in existing_pumpers:
            d = _haversine_miles_one_to_many(p["la"], p["lo"], plant_lats, plant_lons)
            j = int(np.argmin(d))
            owner_type_label = p.get("ot", "Independent")
            # Treat anything not brand-tagged as Independent
            if owner_type_label.startswith("Local:") or owner_type_label.startswith("Regional:"):
                bucket = "Independent"
            elif owner_type_label.startswith("Public:"):
                bucket = owner_type_label
            elif owner_type_label.startswith("PE-Backed:"):
                bucket = owner_type_label.replace("PE-Backed: ", "")
            else:
                bucket = owner_type_label
            rec = {
                "i": p.get("i", ""),
                "n": p.get("n", ""),
                "ad": p.get("ad", ""),
                "c": p.get("c", ""),
                "s": p.get("s", ""),
                "z": p.get("z", ""),
                "la": p["la"],
                "lo": p["lo"],
                "na": p.get("na", ""),
                "op": p.get("op", p.get("n", "")),
                "o": bucket,
                "np": plant_names[j],
                "nd": round(float(d[j]), 1),
                "nplo": plant_owners[j],
                "src": "EPA_FRS_legacy",
            }
            for k in ("ad", "z", "na", "np", "nplo"):
                if rec.get(k) in ("", None):
                    rec.pop(k, None)
            out_records.append(rec)

    os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(out_records, f, separators=(",", ":"))
    sz = os.path.getsize(OUT_JSON)
    print(f"\nWrote {OUT_JSON} ({sz:,} bytes / {len(out_records):,} operators)")

    # Summary
    from collections import Counter
    by_owner = Counter(r["o"] for r in out_records)
    by_state = Counter(r["s"] for r in out_records)
    print()
    print("By owner_type:")
    for k, v in by_owner.most_common():
        print(f"  {v:>5}  {k}")
    print()
    print("Top 20 states:")
    for k, v in by_state.most_common(20):
        print(f"  {v:>5}  {k}")
    if out_records and "nd" in out_records[0]:
        dists = [r["nd"] for r in out_records if r.get("nd") is not None]
        if dists:
            avg = sum(dists) / len(dists)
            far = sum(1 for d in dists if d > 25)
            print(f"\nAvg distance to nearest plant: {avg:.1f} mi")
            print(f"Operators >25 mi from nearest plant: {far:,} "
                  f"({far / len(dists):.1%})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
