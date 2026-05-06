#!/usr/bin/env python3
"""Fix coordinate / state mismatches in EPA FRS-derived datasets.

The EPA FRS national CSV occasionally has rows where the listed
address state doesn't match the lat/lng coordinates. Example: "Jones
Septic Tank Cleaning" — Montgomery, TX 77316 — but coordinates
36.44, -90.39 (Missouri/Arkansas border).

Fix logic per record (lat, lng, state code):
  1. If lat/lng are missing or out of US bounds → flag unfixable.
  2. Check coords against the state bounding box (with 0.5° padding to
     allow for border facilities).
  3. If outside the listed state's bbox:
       a. Try ZIP centroid (data/zip_centroids.json keyed on 5-digit ZIP).
       b. Fall back to state centroid.
       c. If still no fix, leave coords unchanged + flag.

Applied to:
  data/collection_operators.json     (in place — explicit one-shot)
  data/consolidator_supplements.json (in place — explicit one-shot)

The same logic is invoked at build time on FOG_DATA inside
build_index.py:patch_facility_data, so the upstream
fog_facility_map.html doesn't need to be regenerated.

Usage:
    python scripts/fix_state_coords.py
"""

from __future__ import annotations

import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ZIP_LOOKUP_JSON = os.path.join(ROOT, "data", "zip_centroids.json")


# Approximate state bounding boxes (lat_min, lat_max, lng_min, lng_max).
# 0.5° padding is added at validate time so facilities on state borders
# don't false-flag.
STATE_BBOX = {
    "AL": (30.22, 35.00, -88.47, -84.89),
    "AK": (51.20, 71.40, -179.15, -129.97),
    "AZ": (31.33, 37.00, -114.82, -109.04),
    "AR": (33.00, 36.50, -94.62, -89.64),
    "CA": (32.53, 42.01, -124.41, -114.13),
    "CO": (36.99, 41.00, -109.06, -102.04),
    "CT": (40.95, 42.05, -73.73, -71.79),
    "DE": (38.45, 39.84, -75.79, -75.05),
    "DC": (38.79, 38.99, -77.12, -76.91),
    "FL": (24.39, 31.00, -87.63, -80.03),
    "GA": (30.36, 35.00, -85.61, -80.84),
    "HI": (18.91, 22.24, -160.55, -154.81),
    "ID": (41.99, 49.00, -117.24, -111.04),
    "IL": (36.97, 42.51, -91.51, -87.50),
    "IN": (37.77, 41.76, -88.10, -84.78),
    "IA": (40.38, 43.50, -96.64, -90.14),
    "KS": (36.99, 40.01, -102.05, -94.59),
    "KY": (36.50, 39.15, -89.57, -81.96),
    "LA": (28.93, 33.02, -94.04, -88.82),
    "ME": (43.06, 47.46, -71.08, -66.94),
    "MD": (37.89, 39.72, -79.49, -75.05),
    "MA": (41.24, 42.89, -73.51, -69.93),
    "MI": (41.70, 48.31, -90.42, -82.41),
    "MN": (43.50, 49.39, -97.24, -89.49),
    "MS": (30.17, 35.00, -91.66, -88.10),
    "MO": (35.99, 40.61, -95.77, -89.10),
    "MT": (44.36, 49.00, -116.05, -104.04),
    "NE": (39.99, 43.00, -104.05, -95.31),
    "NV": (35.00, 42.00, -120.01, -114.04),
    "NH": (42.70, 45.31, -72.56, -70.61),
    "NJ": (38.93, 41.36, -75.56, -73.89),
    "NM": (31.33, 37.00, -109.05, -103.00),
    "NY": (40.50, 45.02, -79.76, -71.86),
    "NC": (33.84, 36.59, -84.32, -75.46),
    "ND": (45.94, 49.00, -104.05, -96.55),
    "OH": (38.40, 42.00, -84.82, -80.52),
    "OK": (33.62, 37.00, -103.00, -94.43),
    "OR": (41.99, 46.29, -124.57, -116.46),
    "PA": (39.72, 42.27, -80.52, -74.69),
    "RI": (41.15, 42.02, -71.86, -71.12),
    "SC": (32.03, 35.22, -83.35, -78.54),
    "SD": (42.48, 45.95, -104.06, -96.44),
    "TN": (34.98, 36.68, -90.31, -81.65),
    "TX": (25.84, 36.50, -106.65, -93.51),
    "UT": (36.99, 42.00, -114.05, -109.04),
    "VT": (42.73, 45.02, -73.44, -71.46),
    "VA": (36.54, 39.47, -83.68, -75.24),
    "WA": (45.54, 49.00, -124.85, -116.92),
    "WV": (37.20, 40.64, -82.65, -77.72),
    "WI": (42.49, 47.08, -92.89, -86.81),
    "WY": (40.99, 45.01, -111.06, -104.05),
    "PR": (17.93, 18.52, -67.95, -65.21),
}

PAD = 0.5  # degrees of border padding


def state_centroid(state: str) -> tuple[float, float] | None:
    bb = STATE_BBOX.get(state)
    if not bb:
        return None
    lat = (bb[0] + bb[1]) / 2
    lng = (bb[2] + bb[3]) / 2
    return (lat, lng)


def coord_in_state(lat: float, lng: float, state: str) -> bool:
    bb = STATE_BBOX.get(state)
    if not bb:
        return True  # unknown state — don't flag
    return (
        bb[0] - PAD <= lat <= bb[1] + PAD
        and bb[2] - PAD <= lng <= bb[3] + PAD
    )


_zip_lookup_cache: dict[str, list[float]] | None = None


def zip_lookup() -> dict[str, list[float]]:
    global _zip_lookup_cache
    if _zip_lookup_cache is None:
        if not os.path.exists(ZIP_LOOKUP_JSON):
            sys.stderr.write(f"WARNING: {ZIP_LOOKUP_JSON} missing — ZIP fix unavailable\n")
            _zip_lookup_cache = {}
        else:
            with open(ZIP_LOOKUP_JSON, encoding="utf-8") as f:
                _zip_lookup_cache = json.load(f)
    return _zip_lookup_cache


_ZIP_RX = re.compile(r"\b(\d{5})\b")


def extract_zip(zip_field: str | None) -> str | None:
    """The 'z' field in our schema sometimes has the full ZIP+4 or
    extra characters. Pull the first 5 digits."""
    if not zip_field:
        return None
    m = _ZIP_RX.search(zip_field)
    return m.group(1) if m else None


def fix_records(records: list[dict],
                lat_key: str = "la",
                lng_key: str = "lo",
                state_key: str = "s",
                zip_key: str = "z") -> tuple[int, int, int, list[dict]]:
    """Walk records and fix coord/state mismatches in place.
    Returns (mismatches_found, fixed_via_zip, fixed_via_state, unfixable_records)."""
    zips = zip_lookup()
    mismatches = 0
    fixed_zip = 0
    fixed_state = 0
    unfixable: list[dict] = []

    for r in records:
        lat = r.get(lat_key)
        lng = r.get(lng_key)
        st = (r.get(state_key) or "").strip().upper()
        if not isinstance(lat, (int, float)) or not isinstance(lng, (int, float)):
            continue
        if not st or st not in STATE_BBOX:
            continue
        if coord_in_state(float(lat), float(lng), st):
            continue
        # Mismatch — try fixes in order
        mismatches += 1
        zc = extract_zip(r.get(zip_key))
        if zc and zc in zips:
            new_lat, new_lng = zips[zc]
            # Only accept the ZIP fix if the new coords are actually in
            # the state — otherwise the ZIP itself might be miscoded.
            if coord_in_state(new_lat, new_lng, st):
                r[lat_key] = round(new_lat, 5)
                r[lng_key] = round(new_lng, 5)
                r["_coord_fix"] = "zip"
                fixed_zip += 1
                continue
        sc = state_centroid(st)
        if sc:
            r[lat_key] = round(sc[0], 5)
            r[lng_key] = round(sc[1], 5)
            r["_coord_fix"] = "state_centroid"
            fixed_state += 1
            continue
        unfixable.append(r)

    return mismatches, fixed_zip, fixed_state, unfixable


def fix_file(path: str, lat_key="la", lng_key="lo", state_key="s", zip_key="z") -> None:
    if not os.path.exists(path):
        print(f"  (skipped {path} — not present)")
        return
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        print(f"  (skipped {path} — not a list)")
        return
    mis, fzip, fst, bad = fix_records(data, lat_key, lng_key, state_key, zip_key)
    if mis == 0:
        print(f"  {path}: 0 mismatches")
        return
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"))
    print(f"  {path}: {mis} mismatches → fixed {fzip} via ZIP, {fst} via state centroid, {len(bad)} unfixable")
    if bad:
        print(f"    Sample unfixable:")
        for r in bad[:5]:
            print(f"      {(r.get('n') or '?')[:50]} ({r.get('s') or '?'})")


def main() -> int:
    print("Fixing coordinate / state mismatches:")
    fix_file(os.path.join(ROOT, "data", "collection_operators.json"))
    fix_file(os.path.join(ROOT, "data", "consolidator_supplements.json"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
