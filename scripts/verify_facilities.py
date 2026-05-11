#!/usr/bin/env python3
"""AI-powered verification pass for the FOG facility dataset.

For each facility in collection_operators.json + consolidator_supplements.json
this script asks Claude (with the web_search tool) to classify it as one of:

  - CONFIRMED_FOG  — grease trap / septic / liquid waste / UCO / rendering /
                     wastewater treatment business
  - NOT_FOG        — clearly something else (HVAC, plumbing-only, solid waste,
                     construction, auto repair, etc.)
  - UNCLEAR        — can't determine from search results

Modes
-----
    python scripts/verify_facilities.py --sample 200   # stratified audit
    python scripts/verify_facilities.py --full         # every facility
    python scripts/verify_facilities.py --dry-run      # show strata counts only

The script resumes by default: any previously-classified facility (by stable
`id`) is skipped, so re-running after an interruption picks up where it
stopped. Results are written incrementally to data/verification_results.json.

The ANTHROPIC_API_KEY is read from the environment; on macOS we also fall back
to the Keychain entry `ANTHROPIC_API_KEY` (service name) for the current user.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COLLECTION_PATH = os.path.join(ROOT, "data", "collection_operators.json")
RESULTS_PATH = os.path.join(ROOT, "data", "verification_results.json")
FOG_MAP_HTML = os.environ.get(
    "FOG_MAP_HTML",
    os.path.expanduser("~/fog_map_project/fog_facility_map.html"),
)

MODEL = "claude-haiku-4-5-20251001"

# Stratification targets used by --sample. Keys are the user-facing tiers; the
# value lists are the internal bucket codes (matching build_index.py) that
# roll up into each tier.
STRATA = {
    "Local / Family":    (["LOC"],                       75),
    "Regional Operator": (["REG"],                       50),
    "PE-Backed (Other)": (["PE"],                        40),
    "Public Company":    (["PUB"],                       20),
    "Other":             (["LES", "WRE", "BAK", "DAR",
                           "WRM", "MUN", "UNK"],         15),
}


def _bucket_for(o: str | None) -> str:
    """Mirror of scripts/build_index.py::_bucket_for. Maps an operator's
    owner_type string to one of the simplified bucket codes used by the
    map's color/filter scheme."""
    if not o:
        return "LOC"
    if o.startswith("Wind River"):
        return "WRE"
    if o.startswith("LES"):
        return "LES"
    if "Darling" in o:
        return "DAR"
    if o.startswith("Baker"):
        return "BAK"
    if o.startswith("WRM") or "WRM (" in o:
        return "WRM"
    if "Eazy Grease" in o:
        return "REG"
    if o.startswith("Momentum"):
        return "PE"
    if "Septic Blue" in o:
        return "PE"
    if "Barrel" in o:
        return "PUB"
    if "Public:" in o:
        return "PUB"
    if "PE-Backed:" in o or o.startswith("Heritage") or o.startswith("Chuck"):
        return "PE"
    if "Regional:" in o:
        return "REG"
    if "Local:" in o or o == "Independent":
        return "LOC"
    return "PE"


def _load_fog_data() -> list[dict]:
    """Extract the FOG_DATA literal from the upstream map HTML and run the
    same supplement-merge + reclassification chain that scripts/build_index.py
    runs at build time. Returns the post-reclassification plant records.

    We import the reclassify_* functions from build_index instead of
    re-implementing them, so any future change there flows through here
    automatically.
    """
    if not os.path.exists(FOG_MAP_HTML):
        sys.stderr.write(
            f"WARNING: upstream {FOG_MAP_HTML} not found — skipping FOG_DATA layer.\n"
            f"  set FOG_MAP_HTML=/path/to/fog_facility_map.html to include plants.\n"
        )
        return []

    with open(FOG_MAP_HTML, encoding="utf-8") as f:
        html = f.read()
    m = re.search(r"const FOG_DATA\s*=\s*(\[.*?\]);", html, flags=re.DOTALL)
    if not m:
        sys.stderr.write(f"WARNING: FOG_DATA literal not found in {FOG_MAP_HTML}\n")
        return []
    records: list[dict] = json.loads(m.group(1))

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import build_index as bi

    # Merge supplements (only those whose id isn't already in FOG_DATA).
    supplements = bi._load_consolidator_supplements()
    existing_ids = {r.get("i") for r in records if r.get("i")}
    for s in supplements:
        if s.get("i") and s["i"] not in existing_ids:
            records.append(s)
            existing_ids.add(s["i"])

    bi.reclassify_to_public(records)
    bi.reclassify_eazy_grease(records)
    bi.reclassify_wrm(records)
    return records


def load_universe() -> list[dict]:
    """Load and merge FOG_DATA (plants, post-reclassification) and
    collection_operators.json (pumpers/haulers) into one verification list.

    Dedupe is by EPA FRS id. When the same id appears in both layers, the
    plant-layer record wins (richer ct/ot tier info)."""
    fog = _load_fog_data()
    with open(COLLECTION_PATH) as f:
        coll = json.load(f)

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import build_index as bi
    bi.reclassify_collection_to_wrm(coll)

    universe: dict[str, dict] = {}

    for r in fog:
        rid = r.get("i") or ""
        if not rid:
            continue
        # Plant records carry ct directly (LOC/REG/PE/PUB/DAR/WRE/etc.).
        r["_bucket"] = r.get("ct") or _bucket_for(r.get("ot") or r.get("op"))
        r["_layer"] = "plant"
        universe[rid] = r

    for r in coll:
        rid = r.get("i") or ""
        if not rid:
            continue
        if rid in universe:
            # Plant-layer record already covers this facility.
            continue
        r["_bucket"] = _bucket_for(r.get("o"))
        r["_layer"] = "collection"
        universe[rid] = r

    return list(universe.values())


def stratified_sample(records: list[dict], seed: int) -> list[dict]:
    rng = random.Random(seed)
    by_bucket: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_bucket[r["_bucket"]].append(r)

    picked: list[dict] = []
    for tier_name, (buckets, target) in STRATA.items():
        pool: list[dict] = []
        for b in buckets:
            pool.extend(by_bucket.get(b, []))
        rng.shuffle(pool)
        chosen = pool[:target]
        for r in chosen:
            r["_tier"] = tier_name
        picked.extend(chosen)
        if len(chosen) < target:
            sys.stderr.write(
                f"  note: only {len(chosen)} available in '{tier_name}' "
                f"(target was {target})\n"
            )
    return picked


def load_existing_results() -> dict[str, dict]:
    if not os.path.exists(RESULTS_PATH):
        return {}
    with open(RESULTS_PATH) as f:
        data = json.load(f)
    return {r["id"]: r for r in data}


def save_results(results: dict[str, dict]) -> None:
    out = sorted(results.values(), key=lambda r: (r.get("tier", ""), r["id"]))
    tmp = RESULTS_PATH + ".tmp"
    with open(tmp, "w") as f:
        # Compact single-line per the big-data-files convention; the file
        # gets large enough (11k+ records) that pretty-printing balloons it.
        json.dump(out, f, separators=(",", ":"))
    os.replace(tmp, RESULTS_PATH)


def get_api_key() -> str | None:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    if sys.platform != "darwin":
        return None
    try:
        proc = subprocess.run(
            ["security", "find-generic-password", "-a", os.environ.get("USER", ""),
             "-s", "ANTHROPIC_API_KEY", "-w"],
            check=True, capture_output=True, text=True,
        )
        key = proc.stdout.strip()
        if key:
            os.environ["ANTHROPIC_API_KEY"] = key
            return key
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    return None


SYSTEM_PROMPT = """You are a precise business classifier for a FOG (fats, oils, grease) industry intelligence dataset.

A "FOG-related" business is one whose services include ANY of:
  - grease trap cleaning, pumping, or maintenance
  - septic tank pumping, cleaning, hauling, or installation
  - liquid waste hauling (non-hazardous OR mixed haz/non-haz)
  - used cooking oil (UCO) collection / yellow grease / brown grease
  - rendering / animal byproduct processing
  - wastewater treatment, biosolids, sludge handling, sewage processing
  - portable toilet / sanitation services that also pump waste
  - environmental remediation involving liquid waste
  - industrial wastewater pretreatment

A business is NOT FOG-related ONLY if its primary line of work is clearly something else AND it does no liquid-waste service. Examples:
  - pure HVAC contractors (no plumbing)
  - residential plumbers with no septic/grease service
  - pure solid waste / trash haulers (dumpsters, MSW, recycling, landfill) with no liquid arm
  - pure construction, excavation, paving, demolition
  - auto repair, body shops, dealerships, car washes
  - retail, restaurants (UNLESS yellow-grease generators with collection contracts), agriculture
  - oil & gas drilling/production (UNLESS explicitly liquid-waste field service)
  - asbestos abatement, environmental consulting (no hauling)

IMPORTANT EDGE CASES — classify CONFIRMED_FOG:
  - Hazardous-waste handlers that also service liquid/wastewater (Tradebe, Clean Harbors, Veolia, Heritage, Hepaco, Republic Services, GFL): liquid waste is in scope even when haz is also handled.
  - Municipal wastewater treatment plants (WWTP, POTW, water reclamation, sewage treatment, sanitary district): always CONFIRMED_FOG.
  - Drain cleaning companies that ALSO offer septic pumping: CONFIRMED_FOG (check their service menu, not just the name).
  - Any record whose name contains "septic", "wastewater", "grease trap", "UCO", "rendering", or "sewer cleaning" — start from CONFIRMED_FOG and only downgrade to NOT_FOG if the search actively proves otherwise.

Classify each facility into exactly one of:
  CONFIRMED_FOG | NOT_FOG | UNCLEAR

Use web_search to look up the business. Prefer a single combined search per batch when possible. If a search returns no useful info, mark UNCLEAR rather than guessing.

Return ONLY a JSON array of objects with keys: id, classification, reason. The `reason` must be a single short sentence (<=20 words). Do not include any prose outside the JSON array."""


# ============================================================================
# Pre-classification whitelists — run BEFORE any API call. Any facility that
# matches is auto-tagged CONFIRMED_FOG with a whitelist reason and skipped by
# the API loop. Saves cost and prevents the model from making obvious-error
# false negatives (e.g., calling Tradebe / WWTP / Darling plants NOT_FOG).
# ============================================================================

# Known FOG platforms / brands. Match against the `op` or `ot` fields after
# uppercasing. Patterns must be specific enough that they don't grab unrelated
# names (e.g. "Crystal Clean Car Wash" should NOT match Heritage-Crystal Clean).
_FOG_BRAND_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r"\bdarling\b", r"\bdar[\s\-]?pro\b",
        r"\bheritage[\s\-]?crystal\s+clean\b",
        r"\btradebe\b",
        r"\bsynagro\b",
        r"\bdenali\s+water\b",
        r"\bhepaco\b",
        r"\bwind\s+river\b",
        r"\bles\s*\(goldman\b",
        r"\bbaker\s+commodities\b",
        r"\bmahoney\b", r"\bcrimson\b", r"\bneste\b",
        r"\bgfl\s+environmental\b",
        r"\bwaste\s+management\b",
        r"\bclean\s+harbors\b",
        r"\bveolia\b",
        r"\bbarrel\s+energy\b", r"\bhappy\s+traps\b",
        r"\beazy\s+grease\b", r"\bdht\s+grease\b",
        r"\brelentless\s+renewables\b", r"\bdaytona\s+biodiesel\b",
        r"\bwaste\s+resources\s+management\b", r"\bwrm\b",
        r"\bsouth\s*waste\b", r"\bsilver\s+city\s+processing\b",
        r"\bmcdonald\s+farms?\b",
        r"\brestaurant\s+technologies\b",
        r"\bseptic\s+blue\b",
        r"\bmomentum\s+environmental\b",
        r"\bchuck'?s\s+septic\b", r"\bcst\b",
        r"\buniversal\s+environmental\s+services\b",
        r"\bworld\s+oil\s+environmental\b",
        r"\bvalicor\b",
        r"\bascension\s+wastewater\b",
        r"\bnrc\s+environmental\b",
        r"\bonyx\s+environmental\b",
        r"\bus\s+ecology\b", r"\bamerican\s+ecology\b",
        r"\brepublic\s+services\b",
        r"\bstericycle\b",
    ]
]

# Facility-name patterns that are unambiguously FOG (wastewater / septic
# infrastructure). Run against the `n` field uppercased.
_FOG_NAME_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r"\bwastewater\s+treatment\b",
        r"\bwastewater\s+(facility|plant|district|reclamation)\b",
        r"\bwwtp\b", r"\bpotw\b",
        r"\bwater\s+reclamation\b",
        r"\bwater\s+pollution\s+control\b",
        r"\bsewage\s+treatment\b",
        r"\bsanitary\s+sewer\b", r"\bsanitary\s+district\b",
        r"\bgrease\s+trap\b", r"\bgrease\s+(plant|co|company|service)\b",
        r"\brendering\s+(plant|company|services?)?\b",
        r"\bbiosolids\b",
        r"\bused\s+cooking\s+oil\b",
        r"\bporta[\s\-]?potty\b", r"\bportable\s+toilet\b",
        # Strong-signal name fragments — added after first-pass review showed
        # 381 UNCLEAR records had obvious FOG names the model couldn't web-confirm.
        r"\bseptic\s+(tank|service|company|pumping|systems?|cleaning)\b",
        r"\bseptic\s+(co|llc|inc|sves?)\b",
        r"\bsewer\s+(cleaning|service|cleaners?|company)\b",
        r"\bvacuum\s+(truck|service|pumping)\b",
        r"\bliquid\s+waste\s+(hauling|services?|disposal)\b",
        r"\bgrease\s+recyc",
        r"\bcooking\s+oil\s+(collection|recycling|recovery)\b",
        r"\byellow\s+grease\b", r"\bbrown\s+grease\b",
    ]
]


def pre_classify(records: list[dict]) -> dict[str, dict]:
    """Return a {id: result} dict for records that match a whitelist. The
    rest fall through to the API loop. Reason strings explain why each was
    auto-confirmed so the audit log is interpretable."""
    out: dict[str, dict] = {}
    for r in records:
        rid = r.get("i") or ""
        if not rid:
            continue

        name = (r.get("n") or "")
        op_or_ot = (r.get("op") or "") + " " + (r.get("ot") or "")

        reason = None
        # MUN bucket: every facility tagged Municipal at upstream build time is
        # a POTW / wastewater plant by construction.
        if r.get("_bucket") == "MUN":
            reason = "auto: MUN bucket (municipal WWTP)"
        elif any(p.search(name) for p in _FOG_NAME_PATTERNS):
            reason = "auto: facility name matches wastewater/septic pattern"
        elif any(p.search(op_or_ot) for p in _FOG_BRAND_PATTERNS):
            reason = "auto: operator matches known FOG brand"

        if reason:
            out[rid] = {
                "id": rid,
                "name": r.get("n"),
                "operator": r.get("op") or r.get("ot"),
                "city": r.get("c"),
                "state": r.get("s"),
                "bucket": r.get("_bucket"),
                "tier": r.get("_tier"),
                "classification": "CONFIRMED_FOG",
                "reason": reason,
            }
    return out


def build_user_message(batch: list[dict]) -> str:
    lines = []
    for r in batch:
        name = (r.get("n") or "").strip()
        op = (r.get("op") or "").strip()
        city = (r.get("c") or "").strip()
        state = (r.get("s") or "").strip()
        rid = r.get("i") or ""
        # Include the operator name only if it differs from the facility name —
        # it's a useful disambiguator (e.g. "BAKERY FEEDS" operated by Darling).
        if op and op.upper() != name.upper():
            label = f"{name} (operated by {op})"
        else:
            label = name
        lines.append(f"  - id={rid} | {label} | {city}, {state}")
    return "Search the web and classify the following businesses. Return a JSON array.\n\nFacilities:\n" + "\n".join(lines)


def parse_json_array(text: str) -> list[dict]:
    """Best-effort extract of the largest balanced JSON array in `text`."""
    candidates: list[str] = []
    for m in re.finditer(r"\[", text):
        depth = 0
        for j, ch in enumerate(text[m.start():], start=m.start()):
            if ch == "[":
                depth += 1
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


def verify_batch(client, batch: list[dict], max_uses: int) -> list[dict]:
    """One Anthropic API call covering a batch of facilities. Returns a list
    of result dicts (may be shorter than the batch if the model omits some)."""
    user_msg = build_user_message(batch)
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            system=[{
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            tools=[{
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": max_uses,
            }],
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as e:
        sys.stderr.write(f"  API error: {e}\n")
        return []

    text_parts = [b.text for b in response.content if hasattr(b, "text")]
    parsed = parse_json_array("\n".join(text_parts))
    if not parsed:
        sys.stderr.write(
            f"  no parseable JSON. stop_reason={getattr(response, 'stop_reason', '?')}\n"
        )
    return parsed


def run_verification(targets: list[dict], batch_size: int, max_uses: int,
                     resume: bool) -> dict[str, dict]:
    try:
        import anthropic
    except ImportError:
        sys.exit("anthropic package not installed — run: pip install anthropic")

    if not get_api_key():
        sys.exit("ANTHROPIC_API_KEY not available (env + keychain both empty)")

    client = anthropic.Anthropic()

    results = load_existing_results() if resume else {}

    # Whitelist pre-pass: tag obvious confirmeds before the API loop.
    auto = pre_classify([r for r in targets if r["i"] not in results])
    if auto:
        results.update(auto)
        save_results(results)
        print(f"Pre-classified {len(auto)} facilities via whitelist (skipping API).")

    pending = [r for r in targets if r["i"] not in results]
    skipped = len(targets) - len(pending)
    if skipped:
        print(f"Resuming: {skipped} already classified, {len(pending)} to go.")

    total_batches = (len(pending) + batch_size - 1) // batch_size
    for i in range(0, len(pending), batch_size):
        batch = pending[i: i + batch_size]
        bnum = i // batch_size + 1
        print(f"  batch {bnum}/{total_batches}  ({len(batch)} facilities)...", flush=True)
        t0 = time.time()
        verdicts = verify_batch(client, batch, max_uses=max_uses)
        elapsed = time.time() - t0

        verdict_by_id = {str(v.get("id")): v for v in verdicts if isinstance(v, dict)}
        for r in batch:
            rid = r["i"]
            v = verdict_by_id.get(str(rid))
            if v and v.get("classification") in {"CONFIRMED_FOG", "NOT_FOG", "UNCLEAR"}:
                cls = v["classification"]
                reason = v.get("reason", "")[:200]
                # Two-strike guard: if a branded operator slipped past the
                # whitelist (e.g. a new brand) and the model still says
                # NOT_FOG, demote to UNCLEAR rather than auto-delete.
                op_or_ot = (r.get("op") or "") + " " + (r.get("ot") or "")
                if cls == "NOT_FOG" and any(p.search(op_or_ot) for p in _FOG_BRAND_PATTERNS):
                    cls = "UNCLEAR"
                    reason = f"DEMOTED from NOT_FOG (branded operator): {reason}"
                results[rid] = {
                    "id": rid,
                    "name": r.get("n"),
                    "operator": r.get("op") or r.get("ot"),
                    "city": r.get("c"),
                    "state": r.get("s"),
                    "bucket": r.get("_bucket"),
                    "tier": r.get("_tier"),
                    "classification": cls,
                    "reason": reason,
                }
            else:
                results[rid] = {
                    "id": rid,
                    "name": r.get("n"),
                    "operator": r.get("op") or r.get("ot"),
                    "city": r.get("c"),
                    "state": r.get("s"),
                    "bucket": r.get("_bucket"),
                    "tier": r.get("_tier"),
                    "classification": "UNCLEAR",
                    "reason": "no verdict returned",
                }

        save_results(results)
        print(f"    done in {elapsed:.1f}s  (results saved)", flush=True)

    return results


def print_summary(targets: list[dict], results: dict[str, dict],
                  stratified: bool) -> None:
    by_tier: dict[str, Counter] = defaultdict(Counter)
    for r in targets:
        v = results.get(r["i"])
        if not v:
            continue
        tier = r.get("_tier") or {
            "LOC": "Local / Family", "REG": "Regional Operator",
            "PE": "PE-Backed (Other)", "PUB": "Public Company",
        }.get(r["_bucket"], "Other")
        by_tier[tier][v["classification"]] += 1
        by_tier[tier]["__total__"] += 1

    header = "SAMPLE AUDIT RESULTS" if stratified else "FULL VERIFICATION RESULTS"
    print()
    print("=" * 78)
    print(f"{header} ({sum(c['__total__'] for c in by_tier.values())} facilities)")
    print("=" * 78)
    print(f"{'Tier':<22}{'Checked':>9}{'Confirmed':>11}{'Not FOG':>9}{'Unclear':>9}{'FP rate':>10}")
    print("-" * 78)
    order = ["Local / Family", "Regional Operator", "PE-Backed (Other)",
             "Public Company", "Other"]
    totals = Counter()
    for tier in order:
        c = by_tier.get(tier)
        if not c:
            continue
        checked = c["__total__"]
        conf = c["CONFIRMED_FOG"]
        notf = c["NOT_FOG"]
        unc = c["UNCLEAR"]
        fp = (notf / checked * 100) if checked else 0
        print(f"{tier:<22}{checked:>9}{conf:>11}{notf:>9}{unc:>9}{fp:>9.1f}%")
        totals["checked"] += checked
        totals["CONFIRMED_FOG"] += conf
        totals["NOT_FOG"] += notf
        totals["UNCLEAR"] += unc

    print("-" * 78)
    checked = totals["checked"]
    fp = (totals["NOT_FOG"] / checked * 100) if checked else 0
    print(f"{'TOTAL':<22}{checked:>9}{totals['CONFIRMED_FOG']:>11}"
          f"{totals['NOT_FOG']:>9}{totals['UNCLEAR']:>9}{fp:>9.1f}%")
    print()

    # Top NOT_FOG list.
    not_fog = [r for r in results.values() if r["classification"] == "NOT_FOG"]
    not_fog.sort(key=lambda r: (r.get("tier") or "", r.get("state") or "", r.get("name") or ""))
    print(f"Top {min(20, len(not_fog))} NOT_FOG facilities found:")
    print("-" * 78)
    for r in not_fog[:20]:
        loc = f"{r.get('city') or '?'}, {r.get('state') or '?'}"
        print(f"  [{r.get('tier','?')[:18]:<18}] {r.get('name','?'):<35} {loc:<22}")
        print(f"     → {r.get('reason','')}")
    if len(not_fog) > 20:
        print(f"  ... and {len(not_fog) - 20} more (see {os.path.relpath(RESULTS_PATH, ROOT)})")
    print()


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--sample", type=int, help="Stratified sample size (e.g. 200)")
    g.add_argument("--full", action="store_true", help="Verify every facility")
    g.add_argument("--dry-run", action="store_true", help="Print strata counts and exit")
    ap.add_argument("--batch-size", type=int, default=15)
    ap.add_argument("--max-uses", type=int, default=8,
                    help="Cap on web_search tool uses per batch")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--no-resume", action="store_true",
                    help="Ignore existing results file (re-classify from scratch)")
    args = ap.parse_args()

    print(f"Loading dataset...")
    universe = load_universe()
    print(f"  {len(universe)} facilities total")
    bucket_counts = Counter(r["_bucket"] for r in universe)
    print(f"  buckets: {dict(bucket_counts)}")

    if args.dry_run:
        print()
        print("Stratified sample plan:")
        for tier, (buckets, target) in STRATA.items():
            pool = sum(bucket_counts.get(b, 0) for b in buckets)
            print(f"  {tier:<22} target={target:>3}  pool={pool}  buckets={buckets}")
        return 0

    if args.sample:
        targets = stratified_sample(universe, args.seed)
        print(f"Sampled {len(targets)} facilities (seed={args.seed})")
    else:
        # Full pass: tag every record with a tier label for the summary.
        for r in universe:
            r["_tier"] = {
                "LOC": "Local / Family", "REG": "Regional Operator",
                "PE": "PE-Backed (Other)", "PUB": "Public Company",
            }.get(r["_bucket"], "Other")
        targets = universe

    results = run_verification(
        targets, batch_size=args.batch_size, max_uses=args.max_uses,
        resume=not args.no_resume,
    )
    print_summary(targets, results, stratified=bool(args.sample))
    print(f"Detailed per-facility results: {os.path.relpath(RESULTS_PATH, ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
