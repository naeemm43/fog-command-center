#!/usr/bin/env python3
"""One-shot cleanup of data/news_feed.json and data/news_archive.json
using the fuzzy duplicate logic in refresh_data.is_duplicate_news. The
two files are deduplicated together — a story that survived an archival
cycle and re-entered the feed is collapsed onto the archive copy.

Strategy
  1. Tag every record with its source file ("feed" or "archive") plus
     its original index so the report is readable.
  2. Union-find cluster across the combined list using
     refresh_data.is_duplicate_news as the predicate (it sees one
     candidate vs one existing item at a time, but the predicate is
     symmetric enough that we can use it pairwise).
  3. Within each cluster of size >1, keep the most-complete record
     (longest non-empty content across fields), with a tie-break that
     prefers the active feed over the archive so an article that's
     still in-date stays there.
  4. Also backfill `first_seen_date` for any record missing it — we
     don't know exactly when we ingested it, so we use a conservative
     sentinel of the publication date itself. This means existing
     records can never accidentally show up in a future "NEW TODAY"
     section, because their first_seen_date is in the past.

Re-runnable: rewrites both files in place. After the first clean pass
the script should be a no-op.
"""

from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FEED = os.path.join(ROOT, "data", "news_feed.json")
ARCHIVE = os.path.join(ROOT, "data", "news_archive.json")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import refresh_data as rd


def _completeness(item: dict) -> int:
    score = 0
    for v in item.values():
        if isinstance(v, str):
            score += min(len(v.strip()), 400)
        elif isinstance(v, list):
            if v:
                score += 50 + sum(min(len(str(x)), 100) for x in v)
        elif v not in (None, "", False):
            score += 50
    return score


def _short(item: dict, tag: str, idx: int) -> str:
    return (f"[{tag}:{idx:>3}] {item.get('date','?'):<11} | "
            f"{(item.get('headline') or '?')[:80]}")


def main() -> int:
    with open(FEED, encoding="utf-8") as f:
        feed = json.load(f)
    with open(ARCHIVE, encoding="utf-8") as f:
        archive = json.load(f)

    feed_count, archive_count = len(feed), len(archive)
    print(f"Loaded {feed_count} feed items + {archive_count} archive items.")

    combined: list[tuple[str, int, dict]] = []
    for i, n in enumerate(feed):
        combined.append(("feed", i, n))
    for i, n in enumerate(archive):
        combined.append(("archive", i, n))

    n = len(combined)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    # rd.is_duplicate_news takes (item, existing_list). We invoke it
    # pairwise: pair_dup(a, b) === is_duplicate_news(a, [b]).
    for i in range(n):
        ai = combined[i][2]
        for j in range(i + 1, n):
            aj = combined[j][2]
            if rd.is_duplicate_news(ai, [aj]):
                union(i, j)

    clusters: dict[int, list[int]] = {}
    for i in range(n):
        clusters.setdefault(find(i), []).append(i)

    dup_clusters = [c for c in clusters.values() if len(c) > 1]
    keep: set[int] = set()
    for c in clusters.values():
        if len(c) == 1:
            keep.add(c[0])
            continue
        # Tie-break: most complete first; on tie prefer "feed" entries
        # so an in-date article doesn't get collapsed into an archived
        # copy of itself.
        winner = max(c, key=lambda i: (
            _completeness(combined[i][2]),
            1 if combined[i][0] == "feed" else 0,
            -i,
        ))
        keep.add(winner)

    if dup_clusters:
        print()
        print("=" * 78)
        print(f"DUPLICATE CLUSTERS: {len(dup_clusters)}")
        print("=" * 78)
        for c in dup_clusters:
            winner = max(c, key=lambda i: (
                _completeness(combined[i][2]),
                1 if combined[i][0] == "feed" else 0,
                -i,
            ))
            print()
            t, idx, item = combined[winner]
            print(f"  KEEP    {_short(item, t, idx)}")
            for k in c:
                if k == winner:
                    continue
                t, idx, item = combined[k]
                print(f"  REMOVE  {_short(item, t, idx)}")
    else:
        print("No duplicate clusters found.")

    new_feed: list[dict] = []
    new_archive: list[dict] = []
    today_iso = rd.datetime.now(rd.timezone.utc).date().isoformat()
    backfilled_first_seen = 0
    for i in sorted(keep):
        tag, _, item = combined[i]
        # Backfill first_seen_date — never use today, since we don't
        # want these pre-existing records to qualify as new in any
        # future filtering.
        if "first_seen_date" not in item or not item["first_seen_date"]:
            item["first_seen_date"] = item.get("date") or "2000-01-01"
            backfilled_first_seen += 1
        if tag == "feed":
            new_feed.append(item)
        else:
            new_archive.append(item)

    new_feed.sort(key=lambda x: x.get("date") or "1900-01-01", reverse=True)
    new_archive.sort(key=lambda x: x.get("date") or "1900-01-01", reverse=True)

    with open(FEED, "w", encoding="utf-8") as f:
        json.dump(new_feed, f, indent=2)
    with open(ARCHIVE, "w", encoding="utf-8") as f:
        json.dump(new_archive, f, indent=2)

    removed = (feed_count + archive_count) - (len(new_feed) + len(new_archive))
    print()
    print(f"Removed {removed} duplicate entries. "
          f"Feed: {feed_count} → {len(new_feed)}. "
          f"Archive: {archive_count} → {len(new_archive)}.")
    if backfilled_first_seen:
        print(f"Backfilled first_seen_date on {backfilled_first_seen} record(s) "
              f"(set to article publication date — safe sentinel).")
    print(f"Today (UTC): {today_iso}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
