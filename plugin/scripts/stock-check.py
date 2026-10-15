#!/usr/bin/env python3
"""Batch JLCPCB stock / tier / price check — every part in one call.

    stock-check.py AD9235BCPZ-40 C653327 LP5907MFX-3.0      # MPNs and/or LCSC numbers
    stock-check.py --file parts.txt                           # one query per line
    stock-check.py --bom sourcing/sourced_bom.md              # re-verify a whole BOM

The fallback for when the `pcbparts` MCP is not connected. Without it, the measured
sourcing phase ran `curl` against the JLCPCB mirror one part at a time — 55 tool
calls and 53 minutes, each call re-reading the agent's whole context. This queries
every part concurrently and prints one markdown table.

Source: jlcsearch.tscircuit.com, an open mirror of the JLCPCB parts catalogue. It has
stock, price breaks and Basic/Preferred flags. It has NO lifecycle data: EOL/NRND
status is not checked here and must come from the manufacturer or a distributor.

Flags follow the find-part skill's sourcing rules: FAIL below 100 in stock, WARN below
500. A query with no exact MPN/LCSC match lists the closest catalogue hits instead of
guessing.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

API = "https://jlcsearch.tscircuit.com/components/list.json?search="
UA = "Mozilla/5.0 (skidl-skills stock-check)"
FAIL_BELOW, WARN_BELOW = 100, 500


def query(q: str) -> tuple[str, list[dict] | str]:
    try:
        req = urllib.request.Request(API + urllib.parse.quote(q), headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=30) as r:
            return q, json.load(r).get("components") or []
    except Exception as e:                      # network, HTTP, JSON — report, don't die
        return q, f"lookup failed: {e}"


def unit_price(price: str) -> str:
    """First price break of '1-9:18.41,10-29:17.66,...' -> '$18.41'."""
    m = re.match(r"\s*[\d-]+:([\d.]+)", price or "")
    return f"${float(m.group(1)):.3f}" if m else "?"


def tier(c: dict) -> str:
    return "Basic" if c.get("is_basic") else "Preferred" if c.get("is_preferred") else "Extended"


def best_match(q: str, comps: list[dict]) -> dict | None:
    lcsc = re.fullmatch(r"[Cc](\d+)", q)
    for c in comps:
        if lcsc and str(c.get("lcsc")) == lcsc.group(1):
            return c
        if (c.get("mfr") or "").upper() == q.upper():
            return c
    return None


def bom_queries(path: str) -> list[str]:
    """Queries from a markdown BOM table (sourced_bom.md layout).

    Prefers the LCSC# column — JLCPCB's own unambiguous key — and falls back to the MPN
    column. MPNs can contain spaces ("TYPE-C 16PIN 2MD(073)") and summary tables reuse
    an "MPN" header for counts, so an MPN must contain a letter and a digit to count.
    """
    out: list[str] = []
    cols = None
    for line in Path(path).read_text().splitlines():
        if not line.lstrip().startswith("|"):
            cols = None
            continue
        cells = [c.strip().strip("`*").strip() for c in line.strip().strip("|").split("|")]
        if cols is None:
            up = [c.upper().rstrip("#").strip() for c in cells]
            cols = (up.index("MPN") if "MPN" in up else None,
                    up.index("LCSC") if "LCSC" in up else None)
            if cols == (None, None):
                cols = None
            continue
        if all(re.fullmatch(r"[-: ]*", c) for c in cells):
            continue                                  # |---|---| separator row
        mpn_i, lcsc_i = cols
        lcsc = re.findall(r"\bC\d{3,}\b", cells[lcsc_i]) if lcsc_i is not None and lcsc_i < len(cells) else []
        if lcsc:
            picks = lcsc
        elif mpn_i is not None and mpn_i < len(cells):
            mpn = re.sub(r"\s*\(.*?\)\s*$", "", cells[mpn_i]).strip()   # drop "(1k)" notes
            picks = [mpn] if re.search(r"[A-Za-z]", mpn) and re.search(r"\d", mpn) else []
        else:
            picks = []
        for q in picks:
            if q not in out:
                out.append(q)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("queries", nargs="*", help="MPNs and/or LCSC numbers (C123456)")
    ap.add_argument("--file", help="queries, one per line")
    ap.add_argument("--bom", help="markdown BOM with an LCSC# and/or MPN column")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    qs = list(args.queries)
    if args.file:
        qs += [l.strip() for l in Path(args.file).read_text().splitlines()
               if l.strip() and not l.startswith("#")]
    if args.bom:
        qs += bom_queries(args.bom)
    qs = list(dict.fromkeys(qs))                 # de-dup, keep order
    if not qs:
        ap.error("no parts given")

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = dict(pool.map(query, qs))

    print("| Query | MPN | LCSC# | Stock | Unit price | Package | Tier | Flag |")
    print("|---|---|---|---|---|---|---|---|")
    fails = 0
    alternates: list[str] = []
    for q in qs:
        res = results[q]
        if isinstance(res, str):
            print(f"| {q} | — | — | — | — | — | — | ERROR: {res} |")
            fails += 1
            continue
        c = best_match(q, res)
        if c is None:
            print(f"| {q} | — | — | — | — | — | — | NO EXACT MATCH |")
            fails += 1
            near = [f"{x.get('mfr')} (C{x.get('lcsc')}, {x.get('stock')} pcs)" for x in res[:3]]
            if near:
                alternates.append(f"- {q}: closest hits — " + "; ".join(near))
            continue
        stock = int(c.get("stock") or 0)
        flag = ("FAIL <%d" % FAIL_BELOW if stock < FAIL_BELOW
                else "WARN <%d" % WARN_BELOW if stock < WARN_BELOW else "ok")
        fails += stock < FAIL_BELOW
        print(f"| {q} | {c.get('mfr')} | C{c.get('lcsc')} | {stock:,} | {unit_price(c.get('price'))} "
              f"| {c.get('package')} | {tier(c)} | {flag} |")
    if alternates:
        print("\nNo exact match:\n" + "\n".join(alternates))
    print(f"\n{len(qs) - fails}/{len(qs)} parts pass. Source: jlcsearch.tscircuit.com "
          "(JLCPCB mirror) — EOL/NRND status NOT checked.")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
