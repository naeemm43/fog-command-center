#!/usr/bin/env python3
"""Build the restaurant-density heat-map dataset.

Pulls US Census County Business Patterns (CBP) 2022 establishment counts
for NAICS 722 (Food Services and Drinking Places) BY ZIP CODE, joins
against ZIP centroid coordinates, and writes
data/restaurant_density.json — an array of [lat, lng, establishments]
ready for L.heatLayer.

ZIP-level resolution (~18k points after join) gives a continuous heat
surface across metros — neighborhoods are distinguishable, downtown
cores are clearly hotter than suburbs.

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
    "?get=ESTAB,NAICS2017&for=zipcode:*&NAICS2017=722"
)
# erichurst's ZIP→lat/lng gist (US 2013 government data, 33k ZIPs).
# scpike/us-state-county-zip/geo-data.csv was tried first but does not
# include coordinates, only state/county/city.
CENTROIDS_URL = (
    "https://gist.githubusercontent.com/erichurst/7882666/raw/"
    "5bdc46db47d9515269ab12ed6fb2850377fd869e/"
    "US%20Zip%20Codes%20from%202013%20Government%20Data"
)


def fetch_text(url: str) -> str:
    """Fetch a URL via curl with retries — the Census ZIP-level endpoint
    responds slowly on first hit (cold-cache), so allow up to 3 attempts
    with a 5-min timeout each. urllib.request hits a different timeout
    path that's harder to extend cleanly."""
    import subprocess, time
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            r = subprocess.run(
                ["curl", "-sL", "--max-time", "300", "--connect-timeout", "30",
                 "-A", "fog-cmd/1.0", url],
                capture_output=True, check=True, text=True,
            )
            if r.stdout.strip():
                return r.stdout
            raise RuntimeError("empty response body")
        except (subprocess.CalledProcessError, RuntimeError) as e:
            last_err = e
            sys.stderr.write(f"  attempt {attempt+1}/3 failed: {e}\n")
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"all 3 fetch attempts failed: {last_err}")


def main() -> int:
    print(f"Fetching CBP NAICS 722 from Census API (ZIP level)...")
    cbp_raw = fetch_text(CENSUS_URL)
    cbp = json.loads(cbp_raw)
    header, *rows = cbp
    # Header: ["ESTAB","NAICS2017","NAICS2017","zip code"]
    idx_estab = header.index("ESTAB")
    idx_zip = header.index("zip code")

    estab_by_zip: dict[str, int] = {}
    for row in rows:
        try:
            n = int(row[idx_estab])
        except (TypeError, ValueError):
            continue
        z = (row[idx_zip] or "").strip().zfill(5)
        if z:
            estab_by_zip[z] = n
    print(f"  parsed {len(estab_by_zip):,} ZIP rows from CBP")

    print(f"Fetching ZIP centroids from erichurst gist...")
    centroid_raw = fetch_text(CENTROIDS_URL)
    reader = csv.DictReader(io.StringIO(centroid_raw))
    coord_by_zip: dict[str, tuple[float, float]] = {}
    for r in reader:
        z = (r.get("ZIP") or "").strip().zfill(5)
        try:
            lat = float(r["LAT"])
            lng = float(r["LNG"])
        except (TypeError, ValueError, KeyError):
            continue
        coord_by_zip[z] = (lat, lng)
    print(f"  parsed {len(coord_by_zip):,} ZIP centroids")

    # Inner-join on ZIP, drop rows with implausible coords.
    out: list[list[float]] = []
    missing = 0
    for z, n in estab_by_zip.items():
        coord = coord_by_zip.get(z)
        if not coord:
            missing += 1
            continue
        lat, lng = coord
        if not (17 < lat < 72) or not (-180 < lng < -64):
            continue
        # 3-decimal coords are ~110m precision — fine for heat
        # rendering at ZIP density. Halves JSON size vs full precision.
        out.append([round(lat, 3), round(lng, 3), n])

    print(f"  matched {len(out):,} ZIP centroids "
          f"(missing centroid: {missing})")

    os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(out, f, separators=(",", ":"))
    sz = os.path.getsize(OUT_JSON)
    print(f"\nWrote {OUT_JSON} ({sz:,} bytes / {len(out):,} ZIPs)")

    # Print top 10 by establishment count for sanity-check
    out_sorted = sorted(out, key=lambda r: -r[2])[:10]
    print("\nTop 10 ZIPs by NAICS 722 establishment count:")
    for lat, lng, n in out_sorted:
        print(f"  {n:>5,}  ({lat}, {lng})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
