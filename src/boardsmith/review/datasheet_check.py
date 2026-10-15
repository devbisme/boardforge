"""Datasheet check (§4.10 gate 4): each part with an extraction in the part-fact cache,
against the design's exact connectivity (the SKiDL netlist).

- supply: the voltage across the part's supply pins (highest supply-pin rail minus the
  lowest, ground = 0 V) against the datasheet's absolute maximum (error) and recommended
  range (warning). Per power domain when the extraction gives pins a ``power_domain`` (a
  key of ``recommended_operating``, kicad-happy's convention); else one supply, the first of
  SUPPLY_KEYS the extraction has. Spans, not pin-to-ground voltages: an op-amp on +-3.3 V
  sees 6.6 V.
- power pins: a datasheet power-input pin that the design leaves unconnected (error) or puts
  on a signal net (warning).
- extraction: kicad-happy's ``verify_v14_extraction`` consistency checks (warnings).

An extraction scoring under kicad-happy's trust threshold (60) turns errors into warnings.
kicad-happy's own ``datasheet_verify`` also runs inside the C5 schematic analysis.
"""

from boardsmith import datasheets
from boardsmith.review.spice_path import ground_and_rails, rail_volts

SUPPLY_KEYS = ("VS", "VSUPPLY", "VCC", "VDD", "VIN", "V+", "VPWR")
TRUST = 60


def _lim(specs, field):
    """The first non-null ``field`` (min/max) of a SpecValue list, or None."""
    for s in specs or []:
        if isinstance(s, dict) and s.get(field) is not None:
            return float(s[field])
    return None


def _supply_key(ro):
    for k in SUPPLY_KEYS:
        if k in ro:
            return k
    volts = [k for k, v in ro.items() if k.upper().startswith("V") and (v or [{}])[0].get("unit") == "V"]
    return volts[0] if len(volts) == 1 else None


def check_part(comp, ex, rails, ground, dc, verify=None):
    """Findings for one placed part: [{severity: error|warning|info, check, detail}]."""
    ref = comp["ref"]
    base = ex.get("base") or {}
    ro, am = base.get("recommended_operating") or {}, base.get("absolute_max") or {}
    pins = {p["num"]: p.get("net") for p in comp.get("pins", [])}
    out = []

    def volts(net):
        if not net:
            return None
        if net in ground:
            return 0.0
        return rail_volts(net, dc) if net in rails else None

    supply_pins, domains = [], {}
    for pin in base.get("pinout") or []:
        if pin.get("type") != "power_in":
            continue
        for num in pin.get("numbers") or []:
            net = pins.get(str(num))
            label = f"pin {num} ({pin.get('name')})"
            if num not in pins:
                continue  # numbering mismatch: the package check's business
            if not net or "NOCONNECT" in net.upper():
                out.append({"severity": "error", "check": "power_pin",
                            "detail": f"{ref} {label}: datasheet power input left unconnected"})
            elif net not in rails and net not in ground:
                # a warning: datasheets call SW and FB pins "PWR", and extractions copy that
                out.append({"severity": "warning", "check": "power_pin",
                            "detail": f"{ref} {label}: datasheet power input on signal net {net}"})
            else:
                supply_pins.append((label, net))
                if pin.get("power_domain"):
                    domains.setdefault(pin["power_domain"], []).append((label, net))

    gnd_v = [0.0] if any(n in ground for _, n in supply_pins) else []
    has_domains = any(p.get("power_domain") for p in base.get("pinout") or [] if p.get("type") == "power_in")
    domains = {k: v for k, v in domains.items() if any(n not in ground for _, n in v)}
    if not has_domains and any(n not in ground for _, n in supply_pins):
        key = _supply_key(ro)
        if key:
            domains[key] = supply_pins
        else:
            out.append({"severity": "info", "check": "supply",
                        "detail": f"{ref}: no supply key in recommended_operating ({sorted(ro)})"})
    for key, dpins in domains.items():
        vs = [v for v in (volts(n) for _, n in dpins) if v is not None]
        unknown = sorted({n for _, n in dpins if volts(n) is None})
        if unknown:
            out.append({"severity": "info", "check": "supply",
                        "detail": f"{ref} {key}: voltage of {unknown} unknown (set [review] dc)"})
        if not vs:
            continue
        span = round(max(vs) - min(vs + gnd_v), 3)
        nets = ", ".join(sorted({n for _, n in dpins}))
        amax = _lim(am.get(f"{key}_max") or am.get(key), "max")
        lo, hi = _lim(ro.get(key), "min"), _lim(ro.get(key), "max")
        if amax is not None and span > amax:
            out.append({"severity": "error", "check": "supply",
                        "detail": f"{ref} {key} = {span:g} V ({nets}) exceeds absolute max {amax:g} V"})
        elif (lo is not None and span < lo) or (hi is not None and span > hi):
            out.append({"severity": "warning", "check": "supply",
                        "detail": f"{ref} {key} = {span:g} V ({nets}) outside recommended "
                                  f"{lo if lo is not None else '-'}..{hi if hi is not None else '-'} V"})
        else:
            out.append({"severity": "info", "check": "supply",
                        "detail": f"{ref} {key} = {span:g} V within {lo if lo is not None else '-'}.."
                                  f"{hi if hi is not None else '-'} V (abs max {amax if amax is not None else '-'})"})
    if verify:
        for i in verify.verify_v14_extraction(ex):
            out.append({"severity": "warning", "check": "extraction",
                        "detail": f"{ref} extraction {i['path']}: {i['description']}"})
    return trust(out, ex)


def trust(findings, ex):
    """Errors from an extraction under the trust threshold become warnings."""
    q = (ex.get("extraction") or {}).get("quality_score")
    if isinstance(q, (int, float)) and q < TRUST:
        for f in findings:
            if f["severity"] == "error":
                f["severity"] = "warning"
                f["detail"] += f" (extraction quality {q} < {TRUST}: not trusted as an error)"
    return findings


def result(rid, kind, refs, findings):
    """A gate result from findings: HARD if any error; FAIL on an error or warning."""
    sev = {f["severity"] for f in findings}
    return {"id": rid, "kind": kind, "refs": refs, "tag": "HARD" if "error" in sev else "SOFT",
            "status": "FAIL" if sev & {"error", "warning"} else "PASS", "findings": findings}


def check(netlist, store, dc, prefixes):
    """Gate results: one per MPN with an extraction, plus one listing the parts without."""
    verify = datasheets.kh("datasheet_verify")
    ground, rails = ground_and_rails(netlist, dc)
    by_mpn, missing = {}, []
    for comp in netlist["components"]:
        mpn = datasheets.mpn_of(comp)
        if mpn:
            by_mpn.setdefault(mpn, []).append(comp)
    results = []
    for mpn, comps in sorted(by_mpn.items()):
        ex = store.extraction(mpn)
        if ex is None:
            if any(datasheets.ref_prefix(c["ref"]) in prefixes for c in comps):
                missing.append(mpn)
            continue
        findings = [f for c in comps for f in check_part(c, ex, rails, ground, dc, verify)]
        if store.stale(mpn):
            findings.append({"severity": "warning", "check": "extraction",
                             "detail": f"{mpn}: extraction is stale ({store.stale(mpn)})"})
        results.append(result(f"DS:{mpn}", "datasheet", [c["ref"] for c in comps], findings))
    if missing:
        results.append({"id": "DS:coverage", "kind": "datasheet", "tag": "SOFT", "status": "NOT CHECKED",
                        "why": f"{len(missing)} parts have no extraction: {', '.join(missing)}"})
    return results
