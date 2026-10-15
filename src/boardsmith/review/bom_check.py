"""BOM check (§4.10, ported from skidl-skills' ``validate-bom.py``): the sourced BOM CSV
against the design as built (the SKiDL netlist), per reference designator: MPN, LCSC number,
value, footprint, symbol, and the package column against the footprint's body variant.

The value, refdes, symbol and package helpers are skidl-skills' (MIT, Copyright 2026 Nick
Kraakman), copied unchanged; see plugin/ATTRIBUTION.md. A BOM row may cover several parts
("R1,R2", "C10-C29"); every part must be covered exactly once.
"""

import csv
import os
import re

from boardsmith import datasheets
from boardsmith.review.datasheet_check import result

# --- package -> required footprint-name fragments -----------------------------
# Only packages whose NAME does not already appear verbatim in the KiCad
# footprint need an entry. The point of this table is body variants: parts that
# share a pin count and pitch but not a body, which is exactly the class that
# pad-count checking cannot see.
PACKAGE_BODY = {
    "soic-8": ["3.9x4.9"],            # 150 mil narrow body
    "soic-8-150mil": ["3.9x4.9"],
    "soic-8-208mil": ["5.3x5.3"],     # wide body — NOT interchangeable
    "soic-8-wide": ["5.3x5.3"],
    "soic-16-150mil": ["3.9x9.9"],
    "soic-16-300mil": ["7.5x10.3"],
    "tsot-26": ["TSOT-23-6", "TSOT-26"],
    "tsot-23-6": ["TSOT-23-6", "TSOT-26"],
    "sot-23-5": ["SOT-23-5"],
    "sot-23-6": ["SOT-23-6"],
    "msop-8": ["MSOP-8"],
    "vssop-8": ["VSSOP-8"],
}

# Packages that are ambiguous on their own: the BOM must say which body.
AMBIGUOUS = {
    "soic-8": "bare 'SOIC-8' is ambiguous — say 150mil or 208mil",
    "soic-16": "bare 'SOIC-16' is ambiguous — say 150mil or 300mil",
    "sod": "bare 'SOD' is ambiguous — give the full SOD-nnn",
}

_SUFFIX = {
    "p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3,
    "": 1.0, "k": 1e3, "K": 1e3, "meg": 1e6, "M": 1e6, "g": 1e9, "G": 1e9,
}
_VALUE_RE = re.compile(
    r"^\s*([0-9]*\.?[0-9]+)\s*(meg|[pnuµmkKMgG]?)\s*(?:[FfHhΩR]|ohm|ohms)?\s*$"
)


def parse_value(text):
    """Return a float in base units, or None if this is not an R/C/L value.

    Handles '100nF', '0.1uF', '909k', '4.7', '22pF', '3.3uH', '1M'. Also the
    R-notation KiCad and JLC both use ('4k7', '1R0', '0R33').
    """
    if text is None:
        return None
    s = str(text).strip()
    if not s:
        return None
    # R-notation: digit + multiplier letter + digits ('4k7' == 4700)
    m = re.match(r"^\s*([0-9]+)([pnuµmkKMRrG])([0-9]+)\s*$", s)
    if m:
        whole, suf, frac = m.groups()
        if suf in ("R", "r"):
            return float(f"{whole}.{frac}")
        mult = _SUFFIX.get(suf if suf not in ("K",) else "k")
        if mult:
            return float(f"{whole}.{frac}") * mult
    m = _VALUE_RE.match(s)
    if not m:
        return None
    num, suf = m.group(1), m.group(2)
    mult = _SUFFIX.get(suf if suf != "meg" else "meg")
    if mult is None:
        return None
    try:
        return float(num) * mult
    except ValueError:
        return None


def values_agree(bom_val, ckt_val):
    """True when two value strings mean the same thing."""
    if bom_val is None or ckt_val is None:
        return bom_val == ckt_val
    a, b = str(bom_val).strip(), str(ckt_val).strip()
    if a == b:
        return True
    fa, fb = parse_value(a), parse_value(b)
    if fa is not None and fb is not None:
        if fa == fb:
            return True
        # 0.5% tolerance absorbs 4.7k vs 4700 rounding, not 200pF vs 220pF
        return abs(fa - fb) <= 0.005 * max(abs(fa), abs(fb))
    return a.casefold() == b.casefold()


def expand_refdes(field):
    """'R1,R2 C10-C13' -> ['R1','R2','C10','C11','C12','C13'].

    Ranges must share a prefix. A bare token passes through unchanged.
    """
    out = []
    for tok in re.split(r"[,\s;]+", str(field or "").strip()):
        if not tok:
            continue
        m = re.match(r"^([A-Za-z_]+)(\d+)\s*[-–]\s*(?:([A-Za-z_]+))?(\d+)$", tok)
        if m:
            pre, lo, pre2, hi = m.groups()
            if pre2 and pre2 != pre:
                out.append(tok)           # not a range we understand — keep literal
                continue
            lo, hi = int(lo), int(hi)
            if hi < lo or hi - lo > 999:
                out.append(tok)
                continue
            out.extend(f"{pre}{i}" for i in range(lo, hi + 1))
        else:
            out.append(tok)
    return out


def norm_lib(name):
    """'/usr/share/kicad/symbols/Device.kicad_sym' -> 'Device'.

    SKiDL reports a library as a full path for system libraries and as a bare name
    for project-local ones. A BOM writes the bare name either way, so both sides get
    normalised before comparison.
    """
    s = str(name or "").strip()
    if not s:
        return ""
    s = s.replace("\\", "/").rsplit("/", 1)[-1]
    for ext in (".kicad_sym", ".lib"):
        if s.endswith(ext):
            s = s[: -len(ext)]
    return s


def symbols_agree(bom_sym, ckt_sym):
    """Compare 'Lib:Name' pairs with the library normalised on both sides."""
    if not bom_sym or not ckt_sym:
        return True
    def split(s):
        lib, _, nm = str(s).rpartition(":")
        return norm_lib(lib), nm.strip()
    return split(bom_sym) == split(ckt_sym)


def package_check(package, footprint):
    """Return (ok, message). Empty package or footprint -> skipped."""
    if not package or not footprint:
        return True, ""
    key = str(package).strip().casefold().replace(" ", "").replace("_", "-")
    fp = str(footprint).split(":", 1)[-1]
    fp_cf = fp.casefold()

    for amb, why in AMBIGUOUS.items():
        if key == amb:
            return False, why

    if key in PACKAGE_BODY:
        frags = PACKAGE_BODY[key]
        if any(f.casefold() in fp_cf for f in frags):
            return True, ""
        return False, (
            f"package '{package}' expects footprint containing "
            f"{' or '.join(repr(f) for f in frags)}, got '{fp}'"
        )

    # Same package family, different body code -> definite mismatch.
    # 'SOD-523' against a footprint carrying 'SOD-923' is not a judgement call:
    # both name the same family, so the numbers have to agree. This is the check
    # that pad-count validation structurally cannot make.
    fam_re = re.compile(r"([A-Za-z]{2,6})-?(\d{1,4})")
    fp_fams = {}
    for m in fam_re.finditer(fp):
        fp_fams.setdefault(m.group(1).casefold(), set()).add(m.group(2))
    for m in fam_re.finditer(str(package)):
        fam, code = m.group(1).casefold(), m.group(2)
        if fam in fp_fams and code not in fp_fams[fam]:
            other = ", ".join(sorted(fp_fams[fam]))
            return False, (
                f"package '{package}' says {fam.upper()}-{code} but footprint "
                f"'{fp}' is {fam.upper()}-{other}"
            )

    # Default: the package name itself should appear in the footprint name.
    # Normalise 0402/0603-style and SOD-523/SOT-23 style separators.
    plain = key.replace("-", "")
    if plain and plain in fp_cf.replace("-", "").replace("_", ""):
        return True, ""
    return None, f"package '{package}' not evident in footprint '{fp}' — confirm by hand"



def _lcsc(comp):
    for k, v in (comp.get("fields") or {}).items():
        if k.casefold() in ("lcsc", "lcsc part", "lcsc#", "jlcpcb", "jlc") and str(v).strip():
            return str(v).strip()
    return ""


def _symbol(comp):
    lib, _, name = (comp.get("lib_id") or "").partition(":")
    return f"{lib}:{name}" if name else ""


def check(netlist, path):
    """Gate result ``BOM``: errors for disagreements, info for packages to confirm by hand."""
    rel = os.path.basename(os.path.dirname(path)) + "/" + os.path.basename(path)
    if not os.path.exists(path):
        return {"id": "BOM", "kind": "bom", "tag": "SOFT", "status": "NOT CHECKED",
                "why": f"no BOM CSV at {rel} ([review] bom)"}
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    cols = {c.strip().casefold(): c for c in (rows[0].keys() if rows else [])}
    if "refdes" not in cols:
        return {"id": "BOM", "kind": "bom", "tag": "HARD", "status": "FAIL", "refs": [],
                "findings": [{"severity": "error", "check": "bom",
                              "detail": f"{rel} has no rows or no refdes column"}]}

    def cell(row, name):
        key = cols.get(name)
        return (row.get(key) or "").strip() if key else ""

    ckt = {c["ref"]: c for c in netlist["components"] if not c["ref"].startswith("#")}
    out, seen, bad = [], {}, set()

    def err(ref, msg, sev="error"):
        out.append({"severity": sev, "check": "bom", "detail": f"{ref}: {msg}"})
        if sev == "error":
            bad.add(ref)

    for n, row in enumerate(rows, 2):  # line 1 is the header
        for ref in expand_refdes(cell(row, "refdes")):
            if ref in seen:
                err(ref, f"listed twice in the BOM (rows {seen[ref]} and {n})")
                continue
            seen[ref] = n
            if ref not in ckt:
                err(ref, f"in the BOM (row {n}) but not in the design")
                continue
            c = ckt[ref]
            want = cell(row, "mpn")
            have = datasheets.mpn_of(c) or ""
            if want and not have:
                err(ref, f"no MPN field in the design; BOM says '{want}' (row {n})", "warning")
            elif want and want.casefold() != have.casefold():
                err(ref, f"MPN: BOM '{want}' vs design '{have}' (row {n})")
            want = cell(row, "lcsc")
            have = _lcsc(c)
            if want and not have:
                # the fab BOM takes LCSC numbers from the design: JLCPCB can't place the part
                err(ref, f"no LCSC field in the design; BOM says '{want}' (row {n})", "warning")
            elif want and want.casefold() != have.casefold():
                err(ref, f"LCSC: BOM '{want}' vs design '{have}' (row {n})")
            want = cell(row, "value")
            if want and not values_agree(want, c.get("value")):
                err(ref, f"value: BOM '{want}' vs design '{c.get('value')}' (row {n})")
            want = cell(row, "footprint")
            if want and c.get("footprint") and want != c["footprint"]:
                err(ref, f"footprint: BOM '{want}' vs design '{c['footprint']}' (row {n})")
            want = cell(row, "symbol")
            if want and not symbols_agree(want, _symbol(c)):
                err(ref, f"symbol: BOM '{want}' vs design '{_symbol(c)}' (row {n})")
            ok, msg = package_check(cell(row, "package"), cell(row, "footprint") or c.get("footprint"))
            if ok is False:
                err(ref, f"{msg} (row {n})")
            elif ok is None:
                err(ref, f"{msg} (row {n})", "info")
    for ref in sorted(set(ckt) - set(seen)):
        err(ref, "in the design but not in the BOM")
    r = result("BOM", "bom", sorted(bad), out)
    r["why"] = f"{len(ckt)} parts, {len(rows)} BOM rows"
    return r
