#!/usr/bin/env python3
"""Splice the latest data/collection_operators.json into docs/index.html
between the // COLLECTION_DATA_START and // COLLECTION_DATA_END
markers. Used by the supplement workflow so it can update the served
HTML without needing the upstream fog_facility_map.html source (which
isn't available on the GitHub Actions runner).

If the markers aren't present (older HTML build), do nothing.

Usage: python scripts/splice_collection.py
"""

from __future__ import annotations

import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML_PATH = os.path.join(ROOT, "docs", "index.html")
COLLECTION_JSON = os.path.join(ROOT, "data", "collection_operators.json")

START = "// COLLECTION_DATA_START"
END = "// COLLECTION_DATA_END"


def main() -> int:
    if not os.path.exists(HTML_PATH):
        sys.stderr.write(f"docs/index.html missing at {HTML_PATH}\n")
        return 2
    if not os.path.exists(COLLECTION_JSON):
        sys.stderr.write(f"data/collection_operators.json missing\n")
        return 2

    with open(HTML_PATH, encoding="utf-8") as f:
        html = f.read()
    with open(COLLECTION_JSON, encoding="utf-8") as f:
        data = json.load(f)

    pattern = re.compile(
        re.escape(START) + r".*?" + re.escape(END),
        re.DOTALL,
    )
    if not pattern.search(html):
        sys.stderr.write(
            "WARNING: COLLECTION_DATA marker pair not found in HTML. "
            "Run scripts/build_index.py once locally to generate the "
            "marker-wrapped block, then this splice will work.\n"
        )
        return 1

    payload = json.dumps(data, separators=(",", ":"))
    new_block = (
        f"{START}\nconst COLLECTION_DATA = {payload};\n{END}"
    )
    new_html = pattern.sub(lambda _m: new_block, html, count=1)
    with open(HTML_PATH, "w", encoding="utf-8") as f:
        f.write(new_html)
    print(f"Spliced {len(data):,} collection operators into "
          f"{HTML_PATH} ({os.path.getsize(HTML_PATH):,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
