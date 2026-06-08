#!/usr/bin/env python3
"""One-shot addition for Premier Grease (GA/FL collection operator).

Premier Grease (HQ PO Box, Alpharetta GA; est. 2001) is a regional
grease-management service company across four metros:

    Atlanta GA, Savannah GA, Jacksonville FL, Orlando FL

Services: grease trap cleaning, hood cleaning, used cooking oil
collection, and non-hazardous wastewater pumping. ~1,400+ commercial
kitchen accounts.

Plant classification — VERIFIED 2026-06-08 (Claude search):
    - No owned/permitted processing or biodiesel plant identified.
    - UCO page references "our facility" doing filtration but discloses
      no address; GA EPD permit search returned no hits.
    - Classified as COLLECTION OPERATOR only. Flagged for verification
      on whether the filtration site is a transfer location worth a
      separate marker.

This script adds one collection-operator marker per service metro
(centered on the metro coordinate, since Premier Grease publishes no
public facility addresses). Ownership tag is `Premier Grease
(Regional Operator)` — Regional Operator slot because no PE sponsor
has been identified.

Re-runnable: dedupe-by-id catches existing rows.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SUPPLEMENTS_PATH = os.path.join(ROOT, "data", "consolidator_supplements.json")
COLLECTION_PATH = os.path.join(ROOT, "data", "collection_operators.json")
FOG_MAP_HTML = os.environ.get(
    "FOG_MAP_HTML",
    os.path.expanduser("~/fog_map_project/fog_facility_map.html"),
)

# Ownership tag must use the `Regional: <brand>` convention so
# build_index.py's _bucket_for() routes it to the REG bucket. Other
# prefix forms fall through to PE-Backed (Other) by default.
OWNERSHIP_TAG = "Regional: Premier Grease"
OPERATOR_NAME = "Premier Grease & Recycling"

# Metro centroid coordinates (city-hall / downtown lat-lng).
METROS = [
    {
        "i": "PGR_ATL",
        "n": "PREMIER GREASE — ATLANTA SERVICE AREA",
        "ad": "METRO ATLANTA SERVICE AREA",
        "c": "ATLANTA",
        "s": "GA",
        "z": "30303",
        "la": 33.7490,
        "lo": -84.3880,
    },
    {
        "i": "PGR_SAV",
        "n": "PREMIER GREASE — SAVANNAH SERVICE AREA",
        "ad": "METRO SAVANNAH SERVICE AREA",
        "c": "SAVANNAH",
        "s": "GA",
        "z": "31401",
        "la": 32.0809,
        "lo": -81.0912,
    },
    {
        "i": "PGR_JAX",
        "n": "PREMIER GREASE — JACKSONVILLE SERVICE AREA",
        "ad": "METRO JACKSONVILLE SERVICE AREA",
        "c": "JACKSONVILLE",
        "s": "FL",
        "z": "32202",
        "la": 30.3322,
        "lo": -81.6557,
    },
    {
        "i": "PGR_ORL",
        "n": "PREMIER GREASE — ORLANDO SERVICE AREA",
        "ad": "METRO ORLANDO SERVICE AREA",
        "c": "ORLANDO",
        "s": "FL",
        "z": "32801",
        "la": 28.5384,
        "lo": -81.3789,
    },
]

R_MILES = 3958.8


def haversine(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lng1 = math.radians(a[0]), math.radians(a[1])
    lat2, lng2 = math.radians(b[0]), math.radians(b[1])
    dlat = lat2 - lat1
    dlng = lng2 - lng1
    h = (math.sin(dlat / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2)
    return 2 * R_MILES * math.asin(math.sqrt(h))


def load_plant_universe() -> list[dict]:
    plants: list[dict] = []
    if os.path.exists(FOG_MAP_HTML):
        with open(FOG_MAP_HTML, encoding="utf-8") as f:
            html = f.read()
        m = re.search(r"const FOG_DATA\s*=\s*(\[.*?\]);", html, re.DOTALL)
        if m:
            for r in json.loads(m.group(1)):
                if r.get("e") == "plant" \
                        and isinstance(r.get("la"), (int, float)) \
                        and isinstance(r.get("lo"), (int, float)):
                    plants.append(r)
    with open(SUPPLEMENTS_PATH, encoding="utf-8") as f:
        for r in json.load(f):
            if r.get("e") == "plant" \
                    and isinstance(r.get("la"), (int, float)) \
                    and isinstance(r.get("lo"), (int, float)):
                plants.append(r)
    return plants


def nearest_plant(coord: tuple[float, float], plants: list[dict]) -> tuple[dict, float] | None:
    best = None
    best_d = float("inf")
    for p in plants:
        d = haversine(coord, (p["la"], p["lo"]))
        if d < best_d:
            best_d = d
            best = p
    if best is None:
        return None
    return best, best_d


def main() -> int:
    with open(COLLECTION_PATH, encoding="utf-8") as f:
        coll = json.load(f)

    existing_ids = {r.get("i") for r in coll}
    plants = load_plant_universe()
    print(f"Loaded {len(plants)} plants for nearest-plant resolution.")

    # Map id → existing record for in-place updates of ownership tag /
    # operator name. Re-runs reconcile to the current OWNERSHIP_TAG.
    by_id = {r.get("i"): r for r in coll}

    added = 0
    updated = 0
    for marker in METROS:
        existing = by_id.get(marker["i"])
        if existing is not None:
            changes = []
            if existing.get("o") != OWNERSHIP_TAG:
                changes.append(f"o: {existing.get('o')!r} → {OWNERSHIP_TAG!r}")
                existing["o"] = OWNERSHIP_TAG
            if existing.get("op") != OPERATOR_NAME:
                changes.append(f"op: {existing.get('op')!r} → {OPERATOR_NAME!r}")
                existing["op"] = OPERATOR_NAME
            if changes:
                updated += 1
                print(f"  ~ {marker['i']}: " + "; ".join(changes))
            else:
                print(f"  {marker['i']} already present, no changes.")
            continue

        coord = (marker["la"], marker["lo"])
        np_result = nearest_plant(coord, plants)
        if np_result is None:
            np_name, np_dist, np_op = "", None, ""
        else:
            np_rec, np_dist = np_result
            np_name = np_rec.get("n", "")
            np_op = np_rec.get("op", "") or np_rec.get("ot", "")

        record = {
            "i": marker["i"],
            "n": marker["n"],
            "ad": marker["ad"],
            "c": marker["c"],
            "s": marker["s"],
            "z": marker["z"],
            "la": marker["la"],
            "lo": marker["lo"],
            "na": "562998",  # all other miscellaneous waste management services
            "op": OPERATOR_NAME,
            "o": OWNERSHIP_TAG,
            "np": np_name,
            "nd": round(np_dist, 1) if np_dist is not None else None,
            "nplo": np_op,
            "src": "manual_supplement",
            "_verify": (
                "Premier Grease publishes no public facility addresses; "
                "marker placed at metro centroid. Verify whether a "
                "filtration/transfer facility exists at a disclosed "
                "address (UCO page references 'our facility')."
            ),
        }
        coll.append(record)
        added += 1
        np_disp = f"{np_name} ({np_dist:.1f} mi)" if np_dist is not None else "(none)"
        print(f"  + {marker['i']}: {marker['c']}, {marker['s']} → nearest plant: {np_disp}")

    if added == 0 and updated == 0:
        print("No changes (all four metro records already present and current).")
        return 0

    with open(COLLECTION_PATH, "w", encoding="utf-8") as f:
        # collection_operators.json convention is compact single-line.
        json.dump(coll, f, separators=(",", ":"))
    print(f"\nAdded {added} new + updated {updated} existing Premier Grease "
          f"marker(s). collection_operators.json has {len(coll)} records.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
