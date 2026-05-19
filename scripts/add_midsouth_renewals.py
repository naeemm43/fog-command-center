#!/usr/bin/env python3
"""One-shot data addition for the Mid-South Renewals diligence.

  1. Appends a new processing-plant record for Mid-South Renewals
     (2401 Harbor Ave, Memphis, TN 38106) to consolidator_supplements.json.
     The plant is vertically integrated: Greasezilla brown-grease
     conversion, DAF, truck scale. Operating since April 2025. Owner:
     Mike Jones / Mid-South Renewals LLC — privately held, no PE
     sponsor, classified Local/Family.

  2. Recomputes nearest-plant (np / nd / nplo) for every collection
     record whose location is within 30 miles of the new plant. If
     Mid-South Renewals is closer than the record's current nearest
     plant, the np/nd/nplo fields are updated in place.

Re-runnable: a second invocation no-ops because the dedupe check on
the synthetic id catches the existing row.

Coordinates verified via OpenStreetMap Nominatim:
    2401 Harbor Ave, Memphis, TN 38106 → 35.0871376, -90.1206611
"""

from __future__ import annotations

import json
import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SUPPLEMENTS_PATH = os.path.join(ROOT, "data", "consolidator_supplements.json")
COLLECTION_PATH = os.path.join(ROOT, "data", "collection_operators.json")
FOG_MAP_HTML = os.environ.get(
    "FOG_MAP_HTML",
    os.path.expanduser("~/fog_map_project/fog_facility_map.html"),
)

# Stable synthetic id — keeps re-runs idempotent. Prefix MSR_ for any
# future audit trail looking at non-EPA-FRS records.
PLANT_ID = "MSR_2401_HARBOR_MEMPHIS"
PLANT_RECORD = {
    "i": PLANT_ID,
    "n": "MID-SOUTH RENEWALS",
    "ad": "2401 HARBOR AVE",
    "c": "MEMPHIS",
    "s": "TN",
    "z": "38106",
    # Verified 2026-05-19 via OpenStreetMap Nominatim.
    "la": 35.0871376,
    "lo": -90.1206611,
    "na": "562998",  # all other miscellaneous waste management services
    "op": "Mid-South Renewals LLC",
    "ot": "Local: Mid-South Renewals (Mike Jones)",
    "ct": "LOC",
    "e": "plant",
    "nw": "",
    "nd": None,
    "ncs": "",
    "rda": False,
    "dn": "",
    "dd": "",
    "dc": "",
    "_supplemental": True,
    "_notes": (
        "Privately held, vertically integrated FOG processing plant. "
        "Greasezilla brown grease conversion, DAF system, truck scale. "
        "Operating since April 2025. Source: msrtn.com + diligence."
    ),
}

R_MILES = 3958.8


def haversine(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lng1 = math.radians(a[0]), math.radians(a[1])
    lat2, lng2 = math.radians(b[0]), math.radians(b[1])
    dlat = lat2 - lat1
    dlng = lng2 - lng1
    h = (math.sin(dlat / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2)
    return 2 * R_MILES * math.asin(math.sqrt(h))


def _is_plant(rec: dict) -> bool:
    return rec.get("e") == "plant"


def load_plant_universe() -> list[dict]:
    """All plants currently driving nearest-plant calc: FOG_DATA plants
    from the upstream map HTML plus the consolidator supplements file."""
    plants: list[dict] = []
    if os.path.exists(FOG_MAP_HTML):
        import re
        with open(FOG_MAP_HTML, encoding="utf-8") as f:
            html = f.read()
        m = re.search(r"const FOG_DATA\s*=\s*(\[.*?\]);", html, re.DOTALL)
        if m:
            for r in json.loads(m.group(1)):
                if _is_plant(r) and isinstance(r.get("la"), (int, float)) \
                        and isinstance(r.get("lo"), (int, float)):
                    plants.append(r)
    with open(SUPPLEMENTS_PATH, encoding="utf-8") as f:
        for r in json.load(f):
            if _is_plant(r) and isinstance(r.get("la"), (int, float)) \
                    and isinstance(r.get("lo"), (int, float)):
                plants.append(r)
    return plants


def recompute_collection_np(new_plant: dict, recompute_radius_mi: float = 30.0) -> int:
    """For each collection record within `recompute_radius_mi` of the new
    plant, check whether the new plant is closer than the existing
    nearest. If so, rewrite np/nd/nplo in place.

    Returns the count of records updated."""
    with open(COLLECTION_PATH, encoding="utf-8") as f:
        coll = json.load(f)

    new_coord = (new_plant["la"], new_plant["lo"])
    updated: list[tuple[dict, float, float]] = []  # (record, old_nd, new_nd)

    for r in coll:
        la, lo = r.get("la"), r.get("lo")
        if not isinstance(la, (int, float)) or not isinstance(lo, (int, float)):
            continue
        d_new = haversine((la, lo), new_coord)
        if d_new > recompute_radius_mi:
            continue
        d_curr = r.get("nd")
        # If the record had no prior nearest-plant entry OR the new plant
        # beats it, update.
        if d_curr is None or d_new < float(d_curr):
            old_np = r.get("np") or ""
            old_nd = float(d_curr) if d_curr is not None else float("inf")
            r["np"] = new_plant["n"]
            r["nd"] = round(d_new, 1)
            r["nplo"] = new_plant["op"]
            updated.append((r, old_nd, d_new))
            print(f"  update i={r.get('i') or '(no-id)'} {r.get('n','')[:55]} "
                  f"({r.get('c')}, {r.get('s')})")
            print(f"      was: {old_np[:55]} ({old_nd:.1f} mi)" if old_nd != float('inf')
                  else f"      was: (no prior plant assigned)")
            print(f"      now: {new_plant['n']} ({d_new:.1f} mi)")

    with open(COLLECTION_PATH, "w", encoding="utf-8") as f:
        # collection_operators.json convention is compact single-line.
        json.dump(coll, f, separators=(",", ":"))
    return len(updated)


def main() -> int:
    with open(SUPPLEMENTS_PATH, encoding="utf-8") as f:
        sup = json.load(f)

    # Idempotence guard.
    existing = next((r for r in sup if r.get("i") == PLANT_ID), None)
    if existing is not None:
        print(f"Plant {PLANT_ID} already present in supplements; not re-adding.")
    else:
        sup.append(PLANT_RECORD)
        with open(SUPPLEMENTS_PATH, "w", encoding="utf-8") as f:
            # consolidator_supplements.json convention is compact single-line.
            json.dump(sup, f, separators=(",", ":"))
        print(f"Added plant {PLANT_RECORD['n']!r} ({PLANT_RECORD['ad']}, "
              f"{PLANT_RECORD['c']}, {PLANT_RECORD['s']}) → "
              f"supplements ({len(sup)} total).")

    print()
    print("=" * 78)
    print("RECOMPUTING NEAREST PLANT FOR MEMPHIS-METRO COLLECTION RECORDS")
    print("=" * 78)
    n = recompute_collection_np(PLANT_RECORD, recompute_radius_mi=30.0)
    print()
    print(f"Updated {n} collection record(s) whose nearest plant is now Mid-South Renewals.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
