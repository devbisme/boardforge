#!/usr/bin/env python3
"""Check which parts have a KiCad symbol — before coding starts, not during it.

    find-symbol.py AD9235BCPZ-40 THS4551IRGTR CY7C68013A-56LTXC
    find-symbol.py Amplifier_Difference:THS4551xRGT         # exact Lib:Symbol check
    find-symbol.py --file mpns.txt                          # one query per line

In the measured run, three keystone parts (AD9235, GW1NR-9, AD8066) had no symbol in
the KiCad libraries. That surfaced only at coding time, where a block coder spent its
budget generating one and died. Running this during sourcing moves the discovery to
where it is cheap: missing symbols get generated once, from the datasheet pin table
(`pdf-table.py` -> kipart), before any coder starts.

Matching for an MPN, best first:
  exact      symbol name == MPN (case-insensitive)
  wildcard   KiCad's variant convention: a lowercase `x` in a symbol name matches any
             single character (THS4551xRGT ~ THS4551IRGT...), then prefix
  prefix     the symbol name is a leading part of the MPN (CAT24C128 for
             CAT24C128WI-GT3, CY7C68013A-56LTX for CY7C68013A-56LTXC)
  mfr-prefix as above after dropping a vendor prefix KiCad omits (SN74LVC2G34DBVR ->
             74LVC2G34)
A prefix match is only a candidate — the reel/temperature/package suffix it ignores
can change the pinout, so confirm the package in the symbol's footprint filter.

Searches: KiCad 10's symbol directory (as boardsmith finds it; $KICAD10_SYMBOL_DIR
overrides) and ./symbols (project-generated libraries).
Exit status 1 if any query has no candidate at all.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kicadlibs import kicad_dirs  # noqa: E402

# A top-level symbol: `(symbol "NAME"` at the start of a line. Unit sub-symbols are
# named NAME_<unit>_<style> and are filtered out below.
TOP_SYMBOL = re.compile(r'^\s*\(symbol "([^"]+)"', re.M)
UNIT_SUFFIX = re.compile(r"_\d+_\d+$")
# Vendor prefixes KiCad's generic symbols leave off (TI SN74..., ON/Motorola MC74...).
VENDOR_PREFIXES = ("SN", "MC")


def symbol_dirs() -> list[Path]:
    """KiCad's symbol directory (from boardsmith) and the project's symbols/."""
    out = []
    for d in (kicad_dirs()["symbols"], "symbols"):
        p = Path(d).expanduser() if d else None
        if p and p.is_dir() and p.resolve() not in [o.resolve() for o in out]:
            out.append(p)
    return out


def load_index(dirs: list[Path]) -> dict[str, list[str]]:
    """symbol name -> [library names]. Reads .kicad_sym files and KiCad 10's .kicad_symdir
    libraries (a directory of one-symbol .kicad_sym files)."""
    index: dict[str, list[str]] = {}
    for d in dirs:
        files = [(f, f.stem) for f in sorted(d.glob("*.kicad_sym"))]
        files += [(f, lib.stem) for lib in sorted(d.glob("*.kicad_symdir"))
                  for f in sorted(lib.glob("*.kicad_sym"))]
        for f, libname in files:
            try:
                text = f.read_text(errors="replace")
            except OSError:
                continue
            for name in TOP_SYMBOL.findall(text):
                if not UNIT_SUFFIX.search(name) and libname not in index.get(name, []):
                    index.setdefault(name, []).append(libname)
    return index


def wildcard_match(symbol: str, mpn: str) -> bool:
    """KiCad 'x' variant wildcard, matched against the front of the MPN."""
    if "x" not in symbol:
        return False
    pat = "".join("." if c == "x" else re.escape(c) for c in symbol)
    return re.match(pat, mpn, re.I) is not None


def candidates(mpn: str, index: dict[str, list[str]]) -> list[tuple[str, str, str]]:
    """[(kind, lib, symbol)] best first."""
    up = mpn.upper()
    exact, wild, prefix = [], [], []
    for name, libs in index.items():
        n = name.upper()
        if n == up:
            exact += [("exact", lib, name) for lib in libs]
        elif wildcard_match(name, mpn):
            wild += [("wildcard", lib, name) for lib in libs]
        elif len(n) >= 5 and up.startswith(n):
            prefix += [("prefix", lib, name) for lib in libs]
    # longest (most specific) prefix / wildcard first
    wild.sort(key=lambda c: -len(c[2]))
    prefix.sort(key=lambda c: -len(c[2]))
    found = exact + wild + prefix
    if not found:
        for vp in VENDOR_PREFIXES:
            if up.startswith(vp) and len(up) > len(vp) + 4:
                found = [("mfr-prefix", lib, sym)
                         for _, lib, sym in candidates(mpn[len(vp):], index)]
                if found:
                    break
    return found


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("queries", nargs="*", help="MPNs, or Lib:Symbol for an exact check")
    ap.add_argument("--file", help="read queries from a file, one per line")
    ap.add_argument("--max", type=int, default=3, help="candidates shown per MPN (default 3)")
    args = ap.parse_args()

    queries = list(args.queries)
    if args.file:
        queries += [q.strip() for q in Path(args.file).read_text().splitlines()
                    if q.strip() and not q.startswith("#")]
    if not queries:
        ap.error("give at least one MPN or Lib:Symbol")

    dirs = symbol_dirs()
    if not dirs:
        sys.exit("No KiCad symbol directory found — check `boardsmith doctor`")
    index = load_index(dirs)

    missing = 0
    for q in queries:
        if ":" in q and not q.startswith("C:"):
            lib, _, sym = q.partition(":")
            ok = lib in index.get(sym, [])
            print(f"{'OK     ' if ok else 'MISSING'}  {q}")
            missing += not ok
            continue
        c = candidates(q, index)
        if not c:
            print(f"MISSING  {q}  — no symbol; generate one (pdf-table.py -> kipart)")
            missing += 1
            continue
        kind, lib, sym = c[0]
        more = "".join(f"\n           also {k}: {l}:{s}" for k, l, s in c[1:args.max])
        print(f"{kind.upper():<8} {q}  ->  {lib}:{sym}{more}")
    sys.stdout.flush()
    print(f"\n{len(queries) - missing}/{len(queries)} have a candidate symbol "
          f"({len(index):,} symbols indexed from {len(dirs)} dir(s)).", file=sys.stderr)
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
