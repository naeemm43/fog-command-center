#!/usr/bin/env python3
"""Web-search supplement for the collection-operator dataset.

Iterates the top US metros, asks Claude (with web_search) for established
grease-trap / septic / UCO operators in each, dedupes against the
existing data/collection_operators.json (FRS-sourced), and appends new
finds tagged data_source="web_search".

Designed to run via the GitHub Actions workflow .github/workflows/
supplement.yml (which has the API key in secrets), in a single batch
or in slices via the METROS env var. Hitting the 30k-input-tokens/min
rate limit on this account tier is likely if all 75 metros run back to
back, so the workflow defaults to BATCH_SIZE=10 and sleeps between
batches.

Local run (requires ANTHROPIC_API_KEY exported):
    python scripts/supplement_operators.py
    METROS="Indianapolis, IN|Columbus, OH|Cincinnati, OH" \
        python scripts/supplement_operators.py
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import time
from typing import Iterable

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COLLECTION_JSON = os.path.join(ROOT, "data", "collection_operators.json")

TOP_METROS = [
    "New York, NY", "Los Angeles, CA", "Chicago, IL", "Dallas, TX", "Houston, TX",
    "Washington, DC", "Philadelphia, PA", "Miami, FL", "Atlanta, GA", "Boston, MA",
    "Phoenix, AZ", "San Francisco, CA", "Riverside, CA", "Detroit, MI", "Seattle, WA",
    "Minneapolis, MN", "San Diego, CA", "Tampa, FL", "Denver, CO", "St. Louis, MO",
    "Baltimore, MD", "Orlando, FL", "Charlotte, NC", "San Antonio, TX", "Portland, OR",
    "Sacramento, CA", "Pittsburgh, PA", "Las Vegas, NV", "Austin, TX", "Cincinnati, OH",
    "Kansas City, MO", "Columbus, OH", "Indianapolis, IN", "Cleveland, OH", "Nashville, TN",
    "San Jose, CA", "Virginia Beach, VA", "Jacksonville, FL", "Milwaukee, WI", "Providence, RI",
    "Raleigh, NC", "Memphis, TN", "Oklahoma City, OK", "Louisville, KY", "Richmond, VA",
    "New Orleans, LA", "Salt Lake City, UT", "Hartford, CT", "Buffalo, NY", "Birmingham, AL",
    "Rochester, NY", "Grand Rapids, MI", "Tucson, AZ", "Fresno, CA", "Tulsa, OK",
    "Omaha, NE", "Albuquerque, NM", "Boise, ID", "El Paso, TX", "Knoxville, TN",
    "McAllen, TX", "Corpus Christi, TX", "Lubbock, TX", "Amarillo, TX", "Midland, TX",
    "Waco, TX", "Des Moines, IA", "Wichita, KS", "Dayton, OH", "Toledo, OH",
    "Chattanooga, TN", "Lexington, KY", "Colorado Springs, CO", "Madison, WI", "Spokane, WA",
]


def _normalize_name(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"\b(llc|inc|corp|co|company|ltd|incorporated|corporation)\b\.?", "", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _haversine_miles(a: tuple[float, float], b: tuple[float, float]) -> float:
    R = 3958.8
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def is_duplicate_operator(candidate: dict, existing: list[dict]) -> bool:
    if not candidate.get("la") or not candidate.get("lo"):
        return True
    cname = _normalize_name(candidate.get("n", ""))
    if not cname:
        return True
    coord = (candidate["la"], candidate["lo"])
    for e in existing:
        if e.get("la") is None or e.get("lo") is None:
            continue
        if _haversine_miles(coord, (e["la"], e["lo"])) > 5.0:
            continue
        ename = _normalize_name(e.get("n", ""))
        if not ename:
            continue
        # Token overlap as a cheap fuzzy match
        a, b = set(cname.split()), set(ename.split())
        if not a or not b:
            continue
        overlap = len(a & b) / len(a | b)
        if overlap >= 0.7:
            return True
    return False


def search_metro(client, metro: str) -> list[dict]:
    prompt = f"""Search for grease-trap cleaning, septic service, used-cooking-oil collection, and liquid-waste haulers in {metro}. Find the top 15-20 ESTABLISHED commercial operators (not individual handymen, not residential plumbers). Focus on companies with commercial grease-trap or septic-pumping as a core line of business.

For each operator return a JSON object:
- company_name
- address (street address; if only an area is known, omit)
- city
- state (2-letter)
- approximate_latitude (5-decimal float; estimate from address or city)
- approximate_longitude (5-decimal float)
- services (free-form: "grease trap, septic, UCO, drain cleaning, etc.")
- size_indicator (fleet count, employee count, years in business, or review count if visible — else omit)

Skip municipal entities, RCRA/medical waste haulers, and pure-residential plumbers. Use no more than 4 web_search calls. Return ONLY a JSON array, no surrounding prose."""
    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=4096,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        sys.stderr.write(f"  {metro}: API error — {e}\n")
        return []
    text = "\n".join(b.text for b in response.content if hasattr(b, "text"))
    # Find the largest valid JSON array in the response
    candidates = []
    for m in re.finditer(r"\[", text):
        depth = 0
        for j, ch in enumerate(text[m.start():], start=m.start()):
            if ch == "[": depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    candidates.append(text[m.start(): j + 1])
                    break
    candidates.sort(key=len, reverse=True)
    for c in candidates:
        try:
            parsed = json.loads(c)
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            continue
    return []


def coerce(raw: dict, metro: str) -> dict | None:
    name = (raw.get("company_name") or raw.get("name") or "").strip()
    if not name:
        return None
    try:
        lat = float(raw.get("approximate_latitude") or raw.get("latitude"))
        lng = float(raw.get("approximate_longitude") or raw.get("longitude"))
    except (TypeError, ValueError):
        return None
    return {
        "n": name,
        "ad": (raw.get("address") or "").strip() or None,
        "c": (raw.get("city") or metro.split(",")[0].strip()),
        "s": (raw.get("state") or metro.split(",")[-1].strip()),
        "la": round(lat, 5),
        "lo": round(lng, 5),
        "op": name,
        "o": "Independent",
        "src": "web_search",
        "_services": (raw.get("services") or "").strip() or None,
        "_size": (raw.get("size_indicator") or "").strip() or None,
    }


def main() -> int:
    try:
        import anthropic
    except ImportError:
        sys.stderr.write("anthropic not installed — pip install anthropic\n")
        return 2
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.stderr.write("ANTHROPIC_API_KEY not set\n")
        return 2

    metros_env = os.environ.get("METROS")
    if metros_env:
        metros = [m.strip() for m in metros_env.split("|") if m.strip()]
    else:
        metros = list(TOP_METROS)

    batch_size = int(os.environ.get("BATCH_SIZE", "10"))
    sleep_between_batches = int(os.environ.get("BATCH_SLEEP_S", "65"))

    client = anthropic.Anthropic()
    with open(COLLECTION_JSON, encoding="utf-8") as f:
        existing = json.load(f)
    print(f"Existing collection operators: {len(existing):,}")
    print(f"Metros to search: {len(metros)}")
    print(f"Batch size: {batch_size}, sleep between batches: {sleep_between_batches}s")

    found = 0
    added = 0
    duplicates = 0
    for i, metro in enumerate(metros):
        if i and i % batch_size == 0 and sleep_between_batches > 0:
            print(f"--- batch break: sleeping {sleep_between_batches}s ---")
            time.sleep(sleep_between_batches)
        print(f"[{i+1}/{len(metros)}] {metro}")
        results = search_metro(client, metro)
        found += len(results)
        for raw in results:
            cand = coerce(raw, metro)
            if not cand:
                continue
            # Drop helper fields before dedupe (keep them on the saved record)
            if is_duplicate_operator(cand, existing):
                duplicates += 1
                continue
            existing.append(cand)
            added += 1

        # Save incrementally so a rate-limit interruption doesn't lose progress
        with open(COLLECTION_JSON, "w", encoding="utf-8") as f:
            json.dump(existing, f, separators=(",", ":"))

    print()
    print("=" * 60)
    print("WEB-SEARCH SUPPLEMENT COMPLETE")
    print("=" * 60)
    print(f"Metros searched:   {len(metros)}")
    print(f"Operators returned: {found}")
    print(f"  Already in FRS:  {duplicates}")
    print(f"  New added:       {added}")
    print(f"Total operators:   {len(existing):,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
