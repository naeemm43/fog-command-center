#!/usr/bin/env python3
"""Retroactive sync: every entry in comp_database.json must have a
matching M&A article in news_feed.json or news_archive.json. Scans the
deal list and creates synthesized news entries for any orphans.

Uses refresh_data._deal_has_news as the match predicate (target +
acquirer distinctive tokens both present in news headline/summary,
news date within 30 days of deal date) so the live pipeline and this
backfill agree on what "has news coverage" means.

Re-runnable: a second invocation is a no-op once the database is clean.
The output gets sorted by date desc and rewritten in place.

Synthesized entries land in news_feed.json regardless of the deal's
date — they carry `_synthesized_from_deal: true` so a future cleanup
can tell which entries came from this path. They use today's date as
`first_seen_date` so they never accidentally appear in a future "NEW
TODAY" email section.
"""

from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMPS = os.path.join(ROOT, "data", "comp_database.json")
FEED = os.path.join(ROOT, "data", "news_feed.json")
ARCHIVE = os.path.join(ROOT, "data", "news_archive.json")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import refresh_data as rd


def main() -> int:
    with open(COMPS, encoding="utf-8") as f:
        comps = json.load(f)
    with open(FEED, encoding="utf-8") as f:
        feed = json.load(f)
    with open(ARCHIVE, encoding="utf-8") as f:
        archive = json.load(f)

    print(f"Loaded {len(comps)} deals, {len(feed)} feed items, "
          f"{len(archive)} archive items.")

    today_iso = rd.datetime.now(rd.timezone.utc).date().isoformat()
    all_news = feed + archive

    matched = 0
    missing: list[dict] = []
    incomplete: list[dict] = []
    for d in comps:
        if not (d.get("target") or "").strip() or not (d.get("acquirer") or "").strip():
            incomplete.append(d)
            continue
        if rd._deal_has_news(d, all_news):
            matched += 1
        else:
            missing.append(d)

    print()
    print(f"Total deals in database:           {len(comps)}")
    print(f"Deals with existing news coverage: {matched}")
    print(f"Deals missing news coverage:       {len(missing)}")
    if incomplete:
        print(f"Deals with incomplete data (skip): {len(incomplete)}")

    if not missing:
        print("\nNothing to do — every deal already has news coverage.")
        return 0

    print()
    print("=" * 78)
    print("CREATING SYNTHESIZED NEWS ENTRIES")
    print("=" * 78)
    synthesized: list[dict] = []
    for d in missing:
        n = rd.synthesize_news_from_deal(d, today_iso)
        synthesized.append(n)
        feed.append(n)
        print(f"  + {n['date']:<11} {n['headline'][:90]}")

    feed.sort(key=lambda x: x.get("date") or "1900-01-01", reverse=True)
    with open(FEED, "w", encoding="utf-8") as f:
        json.dump(feed, f, indent=2)

    print()
    print(f"News articles created retroactively: {len(synthesized)}")
    print(f"news_feed.json: {len(feed) - len(synthesized)} → {len(feed)} items")
    return 0


if __name__ == "__main__":
    sys.exit(main())
