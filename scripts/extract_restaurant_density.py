#!/usr/bin/env python3
"""Build the restaurant-density heat-map dataset.

Pulls US Census County Business Patterns (CBP) 2022 establishment counts
for NAICS 722 (Food Services and Drinking Places) by county, joins
against county centroid coordinates, and writes
data/restaurant_density.json — an array of [lat, lng, establishments]
ready for L.heatLayer.

No API key required.

    python scripts/extract_restaurant_density.py
"""

from __future__ import annotations

import csv
import io
import json
import os
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_JSON = os.path.join(ROOT, "data", "restaurant_density.json")

CENSUS_URL = (
    "https://api.census.gov/data/2022/cbp"
    "?get=ESTAB,NAICS2017&for=county:*&NAICS2017=722"
)
CENTROIDS_URL = (
    "https://raw.githubusercontent.com/btskinner/spatial/"
    "master/data/county_centers.csv"
)


def fetch_text(url: str) -> str:
    """Fetch a URL via curl. urllib.request kept timing out on the
    Census API; the curl binary is more forgiving with slow TLS
    handshakes and large responses."""
    import subprocess
    r = subprocess.run(
        ["curl", "-sL", "--max-time", "180", "-A", "fog-cmd/1.0", url],
        capture_output=True, check=True, text=True,
    )
    return r.stdout


def main() -> int:
    print(f"Fetching CBP NAICS 722 from Census API...")
    cbp_raw = fetch_text(CENSUS_URL)
    cbp = json.loads(cbp_raw)
    header, *rows = cbp
    # Header is ["ESTAB","NAICS2017","NAICS2017","state","county"].
    idx_estab = header.index("ESTAB")
    idx_state = header.index("state")
    idx_county = header.index("county")

    estab_by_fips: dict[str, int] = {}
    for row in rows:
        try:
            n = int(row[idx_estab])
        except (TypeError, ValueError):
            continue
        st = row[idx_state].zfill(2)
        co = row[idx_county].zfill(3)
        estab_by_fips[st + co] = n
    print(f"  parsed {len(estab_by_fips):,} county rows from CBP")

    print(f"Fetching county centroids from btskinner/spatial...")
    centroid_raw = fetch_text(CENTROIDS_URL)
    reader = csv.DictReader(io.StringIO(centroid_raw))
    coord_by_fips: dict[str, tuple[float, float]] = {}
    for r in reader:
        fips = (r.get("fips") or "").strip().zfill(5)
        try:
            lat = float(r["clat10"])
            lng = float(r["clon10"])
        except (TypeError, ValueError, KeyError):
            continue
        coord_by_fips[fips] = (lat, lng)
    print(f"  parsed {len(coord_by_fips):,} county centroids")

    # Inner-join on FIPS, drop rows with implausible coords.
    out: list[list[float]] = []
    missing = 0
    for fips, n in estab_by_fips.items():
        coord = coord_by_fips.get(fips)
        if not coord:
            missing += 1
            continue
        lat, lng = coord
        if not (17 < lat < 72) or not (-180 < lng < -64):
            continue
        # round to 3 decimals — sufficient for heat-map binning, halves
        # the JSON size vs full precision.
        out.append([round(lat, 3), round(lng, 3), n])

    print(f"  matched {len(out):,} county centroids "
          f"(missing centroid: {missing})")

    os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(out, f, separators=(",", ":"))
    sz = os.path.getsize(OUT_JSON)
    print(f"\nWrote {OUT_JSON} ({sz:,} bytes / {len(out):,} counties)")

    # Print top 10 by establishment count for sanity-check
    out_sorted = sorted(out, key=lambda r: -r[2])[:10]
    print("\nTop 10 counties by NAICS 722 establishment count:")
    for lat, lng, n in out_sorted:
        print(f"  {n:>7,}  ({lat}, {lng})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
