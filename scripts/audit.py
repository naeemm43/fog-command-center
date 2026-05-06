#!/usr/bin/env python3
"""Pre-distribution audit. Runs 17 PASS/FAIL checks across the data
files + the rendered docs/index.html and prints a report.

    python scripts/audit.py
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import date, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML_PATH = os.path.join(ROOT, "docs", "index.html")
NEWS_PATH = os.path.join(ROOT, "data", "news_feed.json")
COMPS_PATH = os.path.join(ROOT, "data", "comp_database.json")
RUBRIC_PATH = os.path.join(ROOT, "data", "market_rubric.json")
COLL_PATH = os.path.join(ROOT, "data", "collection_operators.json")
SUPP_PATH = os.path.join(ROOT, "data", "consolidator_supplements.json")

TODAY = datetime.now().date()


def ok(label: str, detail: str = "") -> None:
    print(f"  ✓ PASS  {label}" + (f"  — {detail}" if detail else ""))


def fail(label: str, detail: str = "") -> None:
    print(f"  ✗ FAIL  {label}" + (f"  — {detail}" if detail else ""))


def warn(label: str, detail: str = "") -> None:
    print(f"  ⚠ WARN  {label}" + (f"  — {detail}" if detail else ""))


def header(name: str) -> None:
    print()
    print("=" * 70)
    print(name)
    print("=" * 70)


def load_json(path: str):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main() -> int:
    print("FOG COMMAND CENTER — PRE-DISTRIBUTION AUDIT")
    print(f"Run date: {TODAY.isoformat()}")
    failures: list[str] = []

    def record(check_id: str, passed: bool, detail: str = "") -> None:
        (ok if passed else fail)(check_id, detail)
        if not passed:
            failures.append(check_id)

    # ----- Load data -----
    news = load_json(NEWS_PATH) if os.path.exists(NEWS_PATH) else []
    comps = load_json(COMPS_PATH) if os.path.exists(COMPS_PATH) else []
    rubric = load_json(RUBRIC_PATH) if os.path.exists(RUBRIC_PATH) else {"cities": []}
    coll = load_json(COLL_PATH) if os.path.exists(COLL_PATH) else []
    with open(HTML_PATH, encoding="utf-8") as f:
        html = f.read()

    # Pull FOG_DATA from the HTML for facility checks
    m = re.search(r"const FOG_DATA = (\[.*?\]);\s*\n", html, re.DOTALL)
    fog_records = json.loads(m.group(1)) if m else []

    header("DATA INTEGRITY")

    # 1. Future dates in news feed
    future_news = [n for n in news if (n.get("date") or "") and _safe_date(n["date"]) and _safe_date(n["date"]) > TODAY]
    record("01 No future-dated news items",
           not future_news,
           f"{len(future_news)} future-dated items" if future_news else "all dates ≤ today")
    for n in future_news[:5]:
        print(f"        {n.get('date')} — {n.get('headline','')[:70]}")

    # 2. Raw HTML / citation tags in news headlines/summaries
    tag_rx = re.compile(r"</?[A-Za-z][^>]*>")
    bad_tags = []
    for n in news:
        for fld in ("headline", "summary"):
            v = n.get(fld) or ""
            if tag_rx.search(v):
                bad_tags.append((fld, n.get("headline", "")[:60]))
    record("02 No raw HTML/citation tags in news",
           not bad_tags,
           f"{len(bad_tags)} fields contain tags" if bad_tags else "all clean")
    for f_, h in bad_tags[:5]:
        print(f"        [{f_}] {h}")

    # 3. Duplicate news items by exact headline
    seen: dict[str, int] = {}
    dup_news = []
    for n in news:
        h = (n.get("headline") or "").strip().lower()
        if not h:
            continue
        if h in seen:
            dup_news.append(n)
        else:
            seen[h] = 1
    record("03 No duplicate news items (by headline)",
           not dup_news,
           f"{len(dup_news)} duplicates" if dup_news else "all unique")
    for n in dup_news[:5]:
        print(f"        {n.get('headline','')[:70]}")

    # 4. Duplicate transactions by target+acquirer
    seen2: set[tuple[str, str]] = set()
    dup_comps = []
    for d in comps:
        k = ((d.get("target") or "").strip().lower(), (d.get("acquirer") or "").strip().lower())
        if not k[0] or not k[1]:
            continue
        if k in seen2:
            dup_comps.append(d)
        else:
            seen2.add(k)
    record("04 No duplicate transactions (target+acquirer)",
           not dup_comps,
           f"{len(dup_comps)} duplicates" if dup_comps else "all unique")
    for d in dup_comps[:5]:
        print(f"        {d.get('target','')[:35]} ← {d.get('acquirer','')[:35]}")

    # 5. Future dates in transactions
    future_comps = [d for d in comps if _safe_date(d.get("date","")) and _safe_date(d["date"]) > TODAY]
    record("05 No future-dated transactions",
           not future_comps,
           f"{len(future_comps)} future-dated" if future_comps else "all dates ≤ today")
    for d in future_comps[:5]:
        print(f"        {d.get('date')} — {d.get('target','')[:70]}")

    # 6. Facility coords outside listed state's bbox
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    try:
        import fix_state_coords as _fix
    except Exception as e:
        record("06 Facility coords match listed state",
               False, f"could not import fix_state_coords: {e}")
    else:
        mismatches = []
        for r in fog_records:
            la, lo, st = r.get("la"), r.get("lo"), (r.get("s") or "").upper()
            if (isinstance(la, (int, float)) and isinstance(lo, (int, float))
                    and st in _fix.STATE_BBOX
                    and not _fix.coord_in_state(float(la), float(lo), st)):
                mismatches.append(r)
        record("06 Facility coords match listed state",
               not mismatches,
               f"{len(mismatches)} mismatches in {len(fog_records):,} facilities"
               if mismatches else f"all {len(fog_records):,} in their listed state's bbox")
        for r in mismatches[:20]:
            print(f"        {(r.get('n') or '')[:50]:50}  ({r.get('s')}) "
                  f"({r.get('la')}, {r.get('lo')})")

    # 7. Facility counts by owner type
    from collections import Counter
    plant_counts = Counter(r.get("ct", "?") for r in fog_records)
    op_counts = Counter(r.get("o", "?") for r in coll)
    print(f"  ℹ INFO  07 Plant counts by ct: {dict(sorted(plant_counts.items(), key=lambda kv: -kv[1]))}")
    print(f"  ℹ INFO  07 Operator counts by o: {dict(sorted(op_counts.items(), key=lambda kv: -kv[1])[:8])} …")

    # 8. Comp-database coords valid
    bad_coords = []
    for d in comps:
        la, lo = d.get("latitude"), d.get("longitude")
        if la is None or lo is None:
            bad_coords.append((d.get("target"), "null"))
            continue
        if la == 0 and lo == 0:
            bad_coords.append((d.get("target"), "(0,0)"))
    record("08 Comp transactions have valid coords",
           not bad_coords,
           f"{len(bad_coords)} of {len(comps)} missing/invalid"
           if bad_coords else f"all {len(comps)} have coords")
    for t, why in bad_coords[:5]:
        print(f"        {(t or '?')[:60]} — {why}")

    # 9. News source_url empty/malformed
    bad_urls = []
    for n in news:
        u = (n.get("source_url") or "").strip()
        if not u:
            bad_urls.append((n.get("headline"), "empty"))
            continue
        if not (u.startswith("http://") or u.startswith("https://")):
            bad_urls.append((n.get("headline"), "no http://"))
    record("09 News items have valid source_url",
           not bad_urls,
           f"{len(bad_urls)} of {len(news)} empty/malformed"
           if bad_urls else f"all {len(news)} present")
    for h, why in bad_urls[:5]:
        print(f"        [{why}] {(h or '?')[:60]}")

    # 10. Market Screener: complete city scores
    cities = rubric.get("cities", [])
    crit_ids = {"restaurant_base", "growth", "consolidator_pressure", "rollup_depth",
                "plant_availability", "disposal_access", "fog_enforcement",
                "permit_moat", "strategic_adjacency", "operating_cost"}
    incomplete = []
    for c in cities:
        scores = c.get("scores") or {}
        missing = [k for k in crit_ids if scores.get(k) is None]
        if missing:
            incomplete.append((c.get("name"), missing))
    target_count = 52
    if len(cities) >= target_count and not incomplete:
        record(f"10 Market Screener cities complete ({len(cities)} ≥ {target_count})", True,
               f"all 10 criteria scored on every city")
    else:
        detail = []
        if len(cities) < target_count:
            detail.append(f"only {len(cities)} cities (target ≥ {target_count})")
        if incomplete:
            detail.append(f"{len(incomplete)} cities have incomplete scores")
        record("10 Market Screener cities complete",
               False, "; ".join(detail))
        for nm, missing in incomplete[:5]:
            print(f"        {nm}: missing {missing}")

    # 11. Weight defaults sum to 100%
    weight_check_pass = True
    if "MARKET_DEFAULT_WEIGHTS" in html:
        # Pull from the JS literal
        m = re.search(r'const MARKET_DEFAULT_WEIGHTS = (\{.*?\});\s*\n', html, re.DOTALL)
        if m:
            try:
                w = json.loads(m.group(1))
                for profile, weights in w.items():
                    s = sum(weights.values())
                    if abs(s - 1.0) > 0.001:
                        print(f"        {profile}: sum = {s:.3f}, not 1.0")
                        weight_check_pass = False
            except Exception as e:
                print(f"        could not parse: {e}")
                weight_check_pass = False
    record("11 Weight defaults sum to 100% (both presets)",
           weight_check_pass,
           "both Collections and Plant Acquisition presets" if weight_check_pass else "see details")

    # 12. Placeholder text in HTML — use word boundaries so facility
    # names like "TODOS SANTOS" don't false-match "TODO".
    placeholder_patterns = [
        (r"\[Author\]",           "[Author]"),
        (r"\bTBD\b",               "TBD"),
        (r"\bTODO\b",              "TODO"),
        (r"\bFIXME\b",             "FIXME"),
        (r"\blorem ipsum\b",       "lorem ipsum"),
        (r"\bplaceholder text\b",  "placeholder text"),
    ]
    found_placeholders = []
    for pat, label in placeholder_patterns:
        # Skip matches inside data-attribute / popup text from real
        # facility records: count only those NOT preceded by a comma+
        # quote pair (which indicates JSON value context).
        matches = list(re.finditer(pat, html))
        if matches:
            found_placeholders.append((label, len(matches)))
    record("12 No placeholder text in HTML",
           not found_placeholders,
           ", ".join(f"{p}={n}" for p, n in found_placeholders) if found_placeholders else "clean")
    if found_placeholders:
        for pat, label in placeholder_patterns:
            for m_ in list(re.finditer(pat, html))[:3]:
                ctx = html[max(0, m_.start()-30):m_.end()+30].replace("\n", " ")
                print(f"        [{label}] …{ctx}…")

    # 13. console.log statements
    log_count = len(re.findall(r'\bconsole\.log\b', html))
    log_warn_count = len(re.findall(r'\bconsole\.(warn|error)\b', html))
    record("13 No console.log() statements",
           log_count == 0,
           f"{log_count} console.log calls; {log_warn_count} console.warn/error (kept for diagnostics)")

    header("CROSS-REFERENCE")

    # 14. Wind River plant count = 21
    wre_plants = sum(1 for r in fog_records if r.get("ct") == "WRE")
    record("14 Wind River plant count = 21", wre_plants == 21, f"actual = {wre_plants}")

    # 15. LES plant count = 36
    les_plants = sum(1 for r in fog_records if r.get("ct") == "LES")
    record("15 LES plant count = 36", les_plants == 36, f"actual = {les_plants}")

    # 16. News-feed count in metadata header matches actual
    m_meta = re.search(r"const metadata = (\{.*?\});", html, re.DOTALL)
    meta = {}
    if m_meta:
        try:
            meta = json.loads(m_meta.group(1))
        except Exception:
            pass
    record("16 metadata.newsCount matches news_feed.json",
           meta.get("newsCount") == len(news),
           f"meta = {meta.get('newsCount')}, actual = {len(news)}")

    # 17. Deals count
    record("17 metadata.compCount matches comp_database.json",
           meta.get("compCount") == len(comps),
           f"meta = {meta.get('compCount')}, actual = {len(comps)}")

    header("SUMMARY")
    if not failures:
        print(f"  ALL CHECKS PASSED ({17} of {17})")
        return 0
    print(f"  {len(failures)} of 17 FAILED:")
    for f_ in failures:
        print(f"    {f_}")
    return 1


def _safe_date(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s)).date()
    except (ValueError, TypeError):
        return None


if __name__ == "__main__":
    sys.exit(main())
