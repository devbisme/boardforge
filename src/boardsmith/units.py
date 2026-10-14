"""Component value parsing (pure Python: used by the orchestrator and the KiCad scripts)."""

import re

PREFIX = {"f": 1e-15, "p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3, "": 1.0,
          "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9}


def farads(value):
    """Capacitance of a value string such as "100nF" or "2.2u"; None if it isn't one."""
    m = re.match(r"^\s*([0-9.]+)\s*([pnuµm]?)F?", value or "")
    if not m:
        return None
    return float(m.group(1)) * {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3, "": 1}[m.group(2)]


def si(value, unit=""):
    """Leading quantity of a value string, in base units: "1k 0.1%" -> 1000, "4k7" -> 4700,
    "15pF C0G" -> 1.5e-11, "49.9" -> 49.9, "3-10pF" -> 6.5e-12 (a trimmer's midpoint),
    "120R@100MHz" -> 120. None if it doesn't start with a number."""
    v = (value or "").strip()
    m = re.match(r"^([0-9.]+)\s*-\s*([0-9.]+)\s*([fpnuµmkKMG]?)", v)
    if m:  # range: midpoint
        return (float(m.group(1)) + float(m.group(2))) / 2 * PREFIX[m.group(3)]
    m = re.match(r"^([0-9]+)([fpnuµmkKMGR])([0-9]+)", v)  # 4k7, 2R2, 1n5
    if m:
        p = 1.0 if m.group(2) == "R" else PREFIX[m.group(2)]
        return float(f"{m.group(1)}.{m.group(3)}") * p
    m = re.match(r"^([0-9.]*[0-9])\s*([fpnuµmkKMG]?)(?:" + re.escape(unit) + r"|R|Ω|ohm)?(?![a-zA-Z])"
                 if unit else r"^([0-9.]*[0-9])\s*([fpnuµmkKMG]?)", v)
    if not m:
        return None
    return float(m.group(1)) * PREFIX[m.group(2)]
