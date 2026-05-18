#!/usr/bin/env python3
"""One-shot retroactive multi-category tagging for every article in
data/news_feed.json and data/news_archive.json.

For each item we run the headline + summary through
refresh_data.classify_categories and write the resulting list into a
new `categories` field. The existing `category` (singular) field is
left alone — it's still the primary badge color and stays in place for
any downstream consumer that hasn't migrated yet.

Re-runnable: a second invocation simply recomputes the same array.
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FEED = os.path.join(ROOT, "data", "news_feed.json")
ARCHIVE = os.path.join(ROOT, "data", "news_archive.json")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import refresh_data as rd


def _retag_list(items: list[dict]) -> tuple[int, int]:
    """Mutates items in place. Returns (gained_extra_tags, unchanged)."""
    gained = 0
    unchanged = 0
    for n in items:
        primary = rd.normalize_category(n.get("category"))
        text = (n.get("headline") or "") + " " + (n.get("summary") or "")
        cats = rd.classify_categories(text, primary=primary)
        n["categories"] = cats
        # Keep `category` consistent — set to first element if it was
        # missing or unrecognized.
        if not n.get("category") or rd.normalize_category(n["category"]) != cats[0]:
            n["category"] = cats[0]
        if len(cats) > 1:
            gained += 1
        else:
            unchanged += 1
    return gained, unchanged


def main() -> int:
    with open(FEED, encoding="utf-8") as f:
        feed = json.load(f)
    with open(ARCHIVE, encoding="utf-8") as f:
        archive = json.load(f)

    feed_gained, feed_single = _retag_list(feed)
    arch_gained, arch_single = _retag_list(archive)

    feed.sort(key=lambda x: x.get("date") or "1900-01-01", reverse=True)
    archive.sort(key=lambda x: x.get("date") or "1900-01-01", reverse=True)

    with open(FEED, "w", encoding="utf-8") as f:
        json.dump(feed, f, indent=2)
    with open(ARCHIVE, "w", encoding="utf-8") as f:
        json.dump(archive, f, indent=2)

    total = len(feed) + len(archive)
    multi = feed_gained + arch_gained
    print()
    print("=" * 78)
    print("MULTI-CATEGORY RETAG")
    print("=" * 78)
    print(f"Total articles processed:                {total}")
    print(f"Articles with multiple category tags:    {multi}")
    print(f"Articles with a single category:         {feed_single + arch_single}")
    print(f"  - feed:    {len(feed)} items, {feed_gained} multi, {feed_single} single")
    print(f"  - archive: {len(archive)} items, {arch_gained} multi, {arch_single} single")

    # Distribution: how many tags per article?
    counts = Counter(len(n.get("categories") or []) for n in (feed + archive))
    print()
    print("Tags-per-article distribution:")
    for k in sorted(counts):
        print(f"  {k} tag{'s' if k != 1 else ' '}: {counts[k]}")

    # 10 sample multi-tag articles for human review.
    print()
    print("Sample of articles with multiple categories (first 10):")
    samples = [n for n in (feed + archive) if len(n.get("categories") or []) > 1][:10]
    for n in samples:
        cats = ", ".join(n.get("categories") or [])
        print(f"  {n.get('date'):<11} [{cats}]")
        print(f"             {n.get('headline','')[:90]}")

    # WRM/Darling sanity check.
    print()
    print("=" * 78)
    print("WRM/DARLING SANITY CHECK")
    print("=" * 78)
    wrm = [n for n in (feed + archive)
            if "wrm" in (n.get("headline") or "").lower()
            and "darling" in (n.get("headline") or "").lower()]
    if not wrm:
        print("No WRM/Darling article found in dataset.")
    else:
        for n in wrm:
            cats = n.get("categories") or []
            print(f"  {n.get('date')} categories={cats}")
            print(f"  headline: {n.get('headline')}")
            have_ma = "M&A" in cats
            have_pub = "Public Co." in cats
            print(f"  has M&A:        {have_ma}  {'✓' if have_ma else '✗'}")
            print(f"  has Public Co.: {have_pub} {'✓' if have_pub else '✗'}")
            if not (have_ma and have_pub):
                print("  WARNING: did not match expected categories")
    return 0


if __name__ == "__main__":
    sys.exit(main())
