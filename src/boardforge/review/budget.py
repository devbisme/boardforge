"""Budget recheck (§4.10 gate 6): each ``rail_current`` constraint (the load the design
puts on a rail) against the rated output current of the part that makes the rail, from the
part-fact cache (``regulator.iout_max``, else a recommended IOUT for load switches).

The source is the part with a power-output pin on the rail, directly or through one series
inductor or ferrite bead (a buck's SW pin feeds the rail through its inductor). Power-output
pins come from the extraction's pinout, else the symbol's pin types. PWR_FLAGs don't count.
Not checked: no source, several sources, or a source with no rating in the cache.
"""

import re

from boardforge import datasheets
from boardforge.review.datasheet_check import trust

SERIES = ("L", "FB")  # ref prefixes of parts a rail passes through


def _rating(ex):
    """(amps, basis): regulator.iout_max (its max, else typ, else min); else, for parts the
    extraction doesn't class as regulators (load switches, eFuses), the lowest maximum of
    an output-current entry in base.recommended_operating (IOUT, IO, ILOAD). (None, None)
    if neither exists."""
    for field in ("max", "typ", "min"):
        for s in ((ex or {}).get("regulator") or {}).get("iout_max") or []:
            if isinstance(s, dict) and s.get(field) is not None:
                return float(s[field]), field
    ro = ((ex or {}).get("base") or {}).get("recommended_operating") or {}
    maxes = [float(v["max"]) for k, specs in ro.items() if re.fullmatch(r"I_?(OUT|O|LOAD)", k, re.I)
             for v in specs or [] if isinstance(v, dict) and v.get("max") is not None
             and v.get("unit") in ("A", None)]
    if maxes:
        return min(maxes), "recommended IOUT"
    return None, None


def sources(netlist, store, net):
    """[(ref, mpn, via)] of parts with a power-output pin on ``net`` (via: the series part or None)."""
    pintypes = {(n["ref"], n["pin"]): n.get("pintype", "") for nt in netlist["nets"] for n in nt["nodes"]}

    def drives(comp, on):
        pins = {p["num"] for p in comp.get("pins", []) if p.get("net") == on}
        ex = store.extraction(datasheets.mpn_of(comp) or "")
        if ex:
            out = {str(x) for p in (ex.get("base") or {}).get("pinout") or []
                   if p.get("type") == "power_out" for x in p.get("numbers") or []}
            return bool(pins & out)
        return any(pintypes.get((comp["ref"], p), "").upper() == "POWER-OUT" for p in pins)

    def on_net(n):
        return [c for c in netlist["components"] if any(p.get("net") == n for p in c.get("pins", []))]

    found = [(c["ref"], datasheets.mpn_of(c), None) for c in on_net(net)
             if not c["ref"].startswith("#") and drives(c, net)]
    for s in on_net(net):
        nets = {p.get("net") for p in s.get("pins", [])} - {net, None}
        if datasheets.ref_prefix(s["ref"]) not in SERIES or len(s.get("pins", [])) != 2 or len(nets) != 1:
            continue
        other = nets.pop()
        found += [(c["ref"], datasheets.mpn_of(c), s["ref"]) for c in on_net(other)
                  if c["ref"] != s["ref"] and not c["ref"].startswith("#") and drives(c, other)]
    return found


def check(netlist, c, store):
    """Gate result for one rail_current constraint."""
    r = {"id": c["id"], "kind": c["kind"], "tag": c["tag"]}
    net, load = c["net"], float(c["max_a"])
    if not any(n["name"] == net for n in netlist["nets"]):
        return dict(r, status="NOT CHECKED", why=f"net {net} is not in the netlist")
    src = sources(netlist, store, net)
    if not src:
        return dict(r, status="NOT CHECKED", why=f"{net}: no power-output pin on it or behind one "
                                                 f"series {'/'.join(SERIES)} (an external supply?)")
    if len(src) > 1:
        return dict(r, status="NOT CHECKED", why=f"{net}: several power sources {[s[0] for s in src]}")
    ref, mpn, via = src[0]
    ex = store.extraction(mpn) if mpn else None
    amps, basis = _rating(ex)
    feed = f"{ref} {mpn or '(no MPN)'}" + (f" via {via}" if via else "")
    if amps is None:
        why = "has no extraction" if ex is None else "extraction has no regulator.iout_max"
        return dict(r, status="NOT CHECKED", refs=[ref], why=f"{net} source {feed} {why}")
    pct = round(100 * load / amps)
    detail = f"{net} load {load:g} A, source {feed} rated {amps:g} A ({basis}): {pct} %"
    if load > amps:
        findings = trust([{"severity": "error", "check": "budget", "detail": detail}], ex)
    else:
        findings = [{"severity": "info", "check": "budget", "detail": detail}]
    sev = findings[0]["severity"]
    return dict(r, status="PASS" if sev == "info" else "FAIL", refs=[ref], findings=findings,
                tag=r["tag"] if sev != "warning" else "SOFT")
