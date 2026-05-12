#!/usr/bin/env python3
"""One-shot cleanup of data/comp_database.json — collapses duplicate
transaction entries that the cold-start dataset accumulated before
refresh_data.py's fuzzy is_duplicate_deal was in place.

Strategy
  1. Build clusters of likely-same deals using the same matching logic
     refresh_data.py now uses for incoming items (see
     refresh_data._strings_match / _dates_within / _locations_within).
  2. Within each cluster of size > 1, keep the most-complete entry —
     scored by total non-empty character count across all fields — and
     drop the rest. Most-complete usually means the entry whose summary
     and notes fields are richest, which is what we want surfaced on
     the map and in the email digest.
  3. Print a pair report so the human review trail is visible (which
     entry was kept, which were dropped, by index in the original file).

Re-runnable: reads and rewrites comp_database.json in place. After the
first clean pass the file should be a fixed point (no further dups).
"""

from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMPS_PATH = os.path.join(ROOT, "data", "comp_database.json")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import refresh_data as rd


def _likely_same(a: dict, b: dict) -> bool:
    """Symmetric variant of refresh_data.is_duplicate_deal. We can't just
    call is_duplicate_deal(a, [b]) because that function returns True for
    records with missing target/acquirer to keep half-formed inbound
    items out of the live db — here we want clustering to be a no-op
    for those, not a true."""
    nt_a = rd._normalize_company(a.get("target"))
    nt_b = rd._normalize_company(b.get("target"))
    na_a = rd._normalize_company(a.get("acquirer"))
    na_b = rd._normalize_company(b.get("acquirer"))
    if not (nt_a and nt_b and na_a and na_b):
        return False

    if not rd._strings_match(na_a, na_b, threshold=0.7):
        return False

    if rd._strings_match(nt_a, nt_b, threshold=0.8):
        return True

    if (rd._strings_match(nt_a, nt_b, threshold=0.5)
            and rd._dates_within(a.get("date"), b.get("date"), 60)):
        return True

    # Location-proximity is a third axis but gated on weak target
    # similarity — see comment in refresh_data.is_duplicate_deal.
    if (rd._strings_match(nt_a, nt_b, threshold=0.3)
            and rd._locations_within(a, b, 50)
            and rd._dates_within(a.get("date"), b.get("date"), 60)):
        return True

    return False


def _completeness(deal: dict) -> int:
    """Score a record by how much real content it carries. Strings
    contribute their length (capped at 400 to avoid one giant `notes`
    field swamping the score). Non-empty lists/numbers contribute a
    flat 50."""
    score = 0
    for v in deal.values():
        if isinstance(v, str):
            score += min(len(v.strip()), 400)
        elif isinstance(v, list):
            if v:
                score += 50 + sum(min(len(str(x)), 100) for x in v)
        elif v not in (None, "", False):
            score += 50
    return score


def cluster(records: list[dict]) -> list[list[int]]:
    """Union-find over record indices. Returns the list of clusters,
    each a list of indices into `records`. Singleton clusters included."""
    parent = list(range(len(records)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    n = len(records)
    for i in range(n):
        for j in range(i + 1, n):
            if _likely_same(records[i], records[j]):
                union(i, j)

    buckets: dict[int, list[int]] = {}
    for i in range(n):
        buckets.setdefault(find(i), []).append(i)
    return list(buckets.values())


def _short(d: dict) -> str:
    return (
        f"{d.get('date','?'):<11} | "
        f"{(d.get('target') or '?')[:50]:<50} → "
        f"{(d.get('acquirer') or '?')[:32]}"
    )


def main() -> int:
    with open(COMPS_PATH, encoding="utf-8") as f:
        comps = json.load(f)
    original_count = len(comps)

    clusters = cluster(comps)
    keep_idx: set[int] = set()
    removed_count = 0
    dup_clusters = []

    for c in clusters:
        if len(c) == 1:
            keep_idx.add(c[0])
            continue
        # Pick the most-complete entry; ties resolved by earliest index.
        winner = max(c, key=lambda i: (_completeness(comps[i]), -i))
        keep_idx.add(winner)
        removed_count += len(c) - 1
        dup_clusters.append((winner, [i for i in c if i != winner]))

    if not dup_clusters:
        print("No duplicates found — comp database is already clean.")
        return 0

    print("=" * 78)
    print(f"DUPLICATE CLUSTERS FOUND: {len(dup_clusters)}")
    print("=" * 78)
    for winner, losers in dup_clusters:
        print()
        print(f"  KEEP    [{winner:>3}] {_short(comps[winner])}")
        for loser in losers:
            print(f"  REMOVE  [{loser:>3}] {_short(comps[loser])}")
    print()
    print("=" * 78)

    cleaned = [comps[i] for i in sorted(keep_idx)]
    # Sort by date desc to match the news/comps convention.
    cleaned.sort(key=lambda x: x.get("date") or "1900-01-01", reverse=True)

    with open(COMPS_PATH, "w", encoding="utf-8") as f:
        json.dump(cleaned, f, indent=2)

    print(f"Removed {removed_count} duplicate entries. "
          f"Comp database now has {len(cleaned)} unique transactions "
          f"(was {original_count}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
