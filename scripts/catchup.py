#!/usr/bin/env python3
"""One-shot historical sweep for FOG / liquid-waste M&A activity.

Runs the broader CATCHUP_QUERIES set from refresh_data.py against the
Anthropic web-search tool and merges the results into news_feed.json and
comp_database.json the same way refresh_data does.

Use cases:
  * Initial population of the comp database beyond the seed set.
  * Periodic deep sweeps when we suspect we've missed a wave of deals.

Run locally:
    ANTHROPIC_API_KEY=sk-ant-... python scripts/catchup.py

Run via GitHub Actions: trigger the "Catch-Up Deal Sweep" workflow
(.github/workflows/catchup.yml) — it has the API key from the repo secret
and will commit anything new it finds.
"""

from __future__ import annotations

import sys

import refresh_data as r


def main() -> int:
    raw = r.search_for_updates(
        queries=r.CATCHUP_QUERIES,
        window_note=(
            "Look across 2023, 2024, 2025, and 2026 — this is a one-time "
            "historical catch-up sweep, not a daily refresh. Surface "
            "every discrete deal you can verify, even small ones. "
            "Do not skip a result because it is from 2023; the goal is "
            "to populate gaps in our database."
        ),
    )
    print(f"Catchup search returned {len(raw)} raw results")

    import json
    with open(r.NEWS_PATH, encoding="utf-8") as f:
        news = json.load(f)
    with open(r.COMPS_PATH, encoding="utf-8") as f:
        comps = json.load(f)

    added_news = 0
    added_deals = 0
    for item in raw:
        n = r.coerce_news_item(item)
        if n and not r.is_duplicate_news(n, news):
            news.append(n)
            added_news += 1
        d = r.coerce_deal_from_news(item)
        if d and not r.is_duplicate_deal(d, comps):
            comps.append(d)
            added_deals += 1

    news.sort(key=lambda x: x.get("date", ""), reverse=True)
    comps.sort(key=lambda x: x.get("date", ""), reverse=True)

    # Catchup deliberately does NOT archive aged news — we want to keep
    # historical context surfaceable for the operator's first browse.
    with open(r.NEWS_PATH, "w", encoding="utf-8") as f:
        json.dump(news, f, indent=2)
    with open(r.COMPS_PATH, "w", encoding="utf-8") as f:
        json.dump(comps, f, indent=2)

    from datetime import datetime, timezone
    metadata = {
        "lastRefreshed": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "newsCount": len(news),
        "compCount": len(comps),
    }
    r.update_html(news, comps, metadata)

    print(
        f"Catchup complete. Added {added_news} news items, {added_deals} deals. "
        f"Total: {len(news)} news, {len(comps)} deals."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
