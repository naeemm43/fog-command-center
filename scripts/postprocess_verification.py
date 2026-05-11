#!/usr/bin/env python3
"""One-shot post-processing of data/verification_results.json after the
first full pass surfaced three classes of issue we agreed to fix:

  1. Stericycle (11 records) was correctly identified as not-pure-FOG but
     should stay in the dataset as Public-tier industry context. We add
     `stericycle` to the brand whitelist and retroactively re-tag those
     records as CONFIRMED_FOG.

  2. 381 UNCLEAR records have FOG-suggestive names (Granada Grease Co,
     C and C Septic, Cities Sewer Service, etc.) that the original
     whitelist patterns missed. The expanded patterns in
     verify_facilities.py now match these. We re-run the whitelist over
     the current results and promote any match.

  3. 24 records came back with "no verdict returned" because the model
     dropped them from its JSON output. We re-classify them via a fresh
     API call.

Re-runnable: this script reads + rewrites the same JSON file in place,
so partial-failure mid-script leaves a consistent state.
"""

from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import verify_facilities as vf

RESULTS = vf.RESULTS_PATH


def reload_results() -> list[dict]:
    with open(RESULTS) as f:
        return json.load(f)


def save(records: list[dict]) -> None:
    tmp = RESULTS + ".tmp"
    with open(tmp, "w") as f:
        json.dump(records, f, separators=(",", ":"))
    os.replace(tmp, RESULTS)


def step1_reupgrade_via_whitelist(records: list[dict]) -> int:
    """Re-run the (updated) whitelist over every UNCLEAR or NOT_FOG record
    and promote matches to CONFIRMED_FOG. Returns number upgraded."""
    n = 0
    for r in records:
        if r["classification"] == "CONFIRMED_FOG":
            continue
        name = r.get("name") or ""
        op = (r.get("operator") or "")
        if any(p.search(name) for p in vf._FOG_NAME_PATTERNS):
            r["classification"] = "CONFIRMED_FOG"
            r["reason"] = (
                "auto: facility name matches FOG pattern (postprocess override)"
            )
            n += 1
            continue
        if any(p.search(op) for p in vf._FOG_BRAND_PATTERNS):
            r["classification"] = "CONFIRMED_FOG"
            r["reason"] = (
                "auto: operator matches known FOG brand (postprocess override)"
            )
            n += 1
    return n


def step2_rerun_no_verdict(records: list[dict]) -> int:
    """Re-classify records whose reason is exactly 'no verdict returned'.
    These got dropped from the model's JSON output on the first pass."""
    targets = [r for r in records if r["reason"] == "no verdict returned"]
    if not targets:
        return 0

    import anthropic
    if not vf.get_api_key():
        sys.exit("ANTHROPIC_API_KEY not available")
    client = anthropic.Anthropic()

    # Need the original record dicts (with city/state/op) — we have them in
    # the results file already; build pseudo-records compatible with
    # verify_facilities.build_user_message.
    pseudo = [{
        "i": r["id"], "n": r["name"], "c": r["city"], "s": r["state"],
        "op": r.get("operator") or "",
    } for r in targets]

    n_updated = 0
    batch_size = 15
    by_id = {r["id"]: r for r in records}
    for i in range(0, len(pseudo), batch_size):
        batch = pseudo[i: i + batch_size]
        print(f"  re-run batch {i // batch_size + 1}: {len(batch)} records...")
        verdicts = vf.verify_batch(client, batch, max_uses=8)
        vmap = {str(v.get("id")): v for v in verdicts if isinstance(v, dict)}
        for p in batch:
            v = vmap.get(str(p["i"]))
            target = by_id[p["i"]]
            if v and v.get("classification") in {"CONFIRMED_FOG", "NOT_FOG", "UNCLEAR"}:
                target["classification"] = v["classification"]
                target["reason"] = (v.get("reason", "")[:200]
                                    + " (re-run)")
                n_updated += 1
    return n_updated


def main() -> int:
    records = reload_results()
    before = {"CONFIRMED_FOG": 0, "NOT_FOG": 0, "UNCLEAR": 0}
    for r in records:
        before[r["classification"]] += 1
    print(f"BEFORE: {before}")

    # Order matters: rerun first (might produce more CONFIRMED via API), then
    # whitelist re-scan (catches anything the model still missed).
    n_rerun = step2_rerun_no_verdict(records)
    save(records)
    print(f"  re-classified via API: {n_rerun}")

    n_upgraded = step1_reupgrade_via_whitelist(records)
    save(records)
    print(f"  promoted via expanded whitelist: {n_upgraded}")

    after = {"CONFIRMED_FOG": 0, "NOT_FOG": 0, "UNCLEAR": 0}
    for r in records:
        after[r["classification"]] += 1
    print(f"AFTER:  {after}")
    print(f"DELTA:  CONFIRMED +{after['CONFIRMED_FOG'] - before['CONFIRMED_FOG']}, "
          f"NOT_FOG {after['NOT_FOG'] - before['NOT_FOG']:+d}, "
          f"UNCLEAR {after['UNCLEAR'] - before['UNCLEAR']:+d}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
