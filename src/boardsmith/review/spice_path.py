"""SPICE path gate (§4.10): the AC gain of a whole signal path, from the SKiDL netlist.

The testbench takes connectivity from boardsmith's netlist JSON (not from re-parsed KiCad
files): every resistor, capacitor and inductor as itself, ferrites and polyfuses as
near-shorts, rails as DC sources, the constraint's ``in`` net driven by an AC source.
Op-amps use kicad-happy's behavioral model generator; fully-differential amplifiers get a
small behavioral model here (kicad-happy has none). Model specs come from ``facts`` (the
part-fact cache: {MPN: {gbw_hz, slew_vus, source}}) over ``models`` ([review.models]:
{regex on value / MPN / symbol: {type: opamp|fda, gbw_hz, aol_db, slew_vus, pins}}).
Diodes, LEDs, crystals and connectors are left out (listed in the notes); ICs without a model
are open circuits, and a path that has to cross one is reported as not checked.
"""

import importlib.util
import math
import os
import re
import shutil
import subprocess
import tempfile

from boardsmith.datasheets import mpn_of
from boardsmith.units import si

_KH_SPICE = os.path.join(os.path.dirname(__file__), "..", "vendor", "kicad_happy", "skills", "spice",
                         "scripts", "spice_model_generator.py")

PASSIVE = {"R": ("R", "R"), "R_Small": ("R", "R"), "R_US": ("R", "R"), "C": ("C", "F"),
           "C_Small": ("C", "F"), "C_Polarized": ("C", "F"), "C_Polarized_Small": ("C", "F"),
           "C_Trim": ("C", "F"), "L": ("L", "H"), "L_Small": ("L", "H")}
SHORT = ("FerriteBead", "Polyfuse", "Fuse", "Jumper", "NetTie")
GROUND = re.compile(r"^(GND|AGND|DGND|PGND|VSS|0V)\w*$", re.I)
OPAMP_ROLES = {"inp": ("+", "IN+", "+IN", "INP", "VIN+"), "inn": ("-", "IN-", "-IN", "INN", "VIN-"),
               "vcc": ("V+", "VCC", "VS+", "V_{S+}", "VDD", "+VS"),
               "vee": ("V-", "VEE", "VS-", "V_{S-}", "VSS", "-VS", "GND"),
               "vocm": ("V_{OCM}", "VOCM", "OCM"),
               "outp": ("OUT+", "+OUT", "VOUT+", "OUTP"), "outn": ("OUT-", "-OUT", "VOUT-", "OUTN"),
               "out": ("OUT", "VOUT", "~")}
FDA_PINS = ("inp", "inn", "outp", "outn", "vocm", "vcc", "vee")
OPAMP_PINS = ("inp", "inn", "out", "vcc", "vee")


def _kh_generator():
    """kicad-happy's spice_model_generator (it imports its siblings by bare name)."""
    import sys

    d = os.path.dirname(os.path.abspath(_KH_SPICE))
    sys.path.insert(0, d)
    try:
        spec = importlib.util.spec_from_file_location("kh_spice_model_generator", _KH_SPICE)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(d)
    return mod


def symbol_pins(netlist):
    """{lib_id: {pin number: (name, electrical type)}} from the embedded symbols."""
    out = {}
    for lib_id, sym in netlist.get("symbols", {}).items():
        pins = {}
        for cmds in sym.get("draw_cmds", {}).values():
            for c in cmds:
                if c and c[0] == "pin":
                    kv = {x[0]: x[1] for x in c if isinstance(x, list) and len(x) > 1}
                    pins[str(kv.get("number"))] = (kv.get("name", ""), c[1])
        out[lib_id] = pins
    return out


def rail_volts(name, dc):
    """DC voltage of a rail net from the overrides or its name (+3V3 -> 3.3, -3V3A -> -3.3,
    +1V2 -> 1.2, +5V -> 5, VBUS -> 5). None if unknown."""
    if name in dc:
        return float(dc[name])
    base = name.split("/")[-1]
    if base.upper() == "VBUS":
        return 5.0
    m = re.match(r"^([+-])?(\d+)V(\d+)?", base)
    if not m:
        return None
    v = float(f"{m.group(2)}.{m.group(3) or 0}")
    return -v if m.group(1) == "-" else v


def find_model(comp, models):
    keys = [comp.get("value", ""), comp.get("fields", {}).get("MPN", ""), comp.get("lib_id", "")]
    for rx, m in models.items():
        if any(re.search(rx, k) for k in keys if k):
            return m
    return None


def _roles(comp, pins, model):
    """{role: pin number} for a modelled amplifier: names first, then the model's ``pins``."""
    roles = {}
    for num, (name, etype) in pins.items():
        for role, names in OPAMP_ROLES.items():
            if name in names and role not in roles:
                roles[role] = num
        if role_is_out(name, etype) and "out" not in roles:
            roles["out"] = num
    for role, num in (model.get("pins") or {}).items():
        roles[role] = str(num)
    return roles


def role_is_out(name, etype):
    return etype == "output" and name in ("", "~", "OUT", "VOUT")


def _fda_subckt(name, gbw, aol_db):
    """Fully-differential amplifier: one-pole differential gain, ideal common-mode loop
    (outputs at VOCM +- half the differential output)."""
    aol = 10 ** (aol_db / 20)
    c_pole = 1.0 / (2 * math.pi * gbw / aol)
    return f"""\
.subckt {name} inp inn outp outn vocm vcc vee
* Behavioral FDA (boardsmith): GBW={gbw / 1e6:.1f}MHz, Aol={aol_db}dB
Rin inp inn 1e9
E1 d 0 inp inn {aol:.0f}
R1 d dp 1
C1 dp 0 {c_pole:.6g}
Bp outp 0 V=V(vocm)+V(dp)/2
Bn outn 0 V=V(vocm)-V(dp)/2
.ends {name}
"""


def _node(net, ground_nets):
    if net in ground_nets:
        return "0"
    # "+3V3A" and "-3V3A" must stay different nodes
    return "n_" + re.sub(r"[^A-Za-z0-9_]", "_", net.replace("+", "p").replace("-", "m"))


def testbench(netlist, c, models, dc=None, source_ohms=0.0, facts=None):
    """(spice text, notes, problem). ``problem`` is a reason the path can't be checked."""
    dc, facts = dc or {}, facts or {}
    pins_of = symbol_pins(netlist)
    nets = {n["name"]: n for n in netlist["nets"]}
    outs = c["out"] if isinstance(c["out"], list) else [c["out"]]
    for n in [c["in"], *outs]:
        if n not in nets:
            return None, [], f"net {n!r} isn't in the netlist"
    ground = {n for n in nets if GROUND.match(n.split("/")[-1])}
    power_pin = {n["name"] for n in netlist["nets"]
                 if any(x.get("pintype", "").upper().replace("-", "_") in ("POWER_IN", "POWER_OUT")
                        for x in n["nodes"])}
    rails = (power_pin | set(dc)) - ground - {n["name"] for n in netlist["nets"] if n.get("nc")}
    rails = {n for n in rails if "NOCONNECT" not in n.upper()}
    lines, subckts, notes, omitted, unmodelled = [], {}, [], [], []
    edges = []  # (net, net) the signal can cross
    gen = None
    for comp in netlist["components"]:
        ref, lib_id = comp["ref"], comp.get("lib_id", "")
        if ref.startswith("#"):
            continue
        name = lib_id.split(":")[-1]
        pn = {p["num"]: p["net"] for p in comp["pins"] if p.get("net")}
        if name in PASSIVE and len(pn) == 2:
            kind, unit = PASSIVE[name]
            v = si(comp.get("value", ""), unit)
            a, b = list(pn.values())
            if v is None:
                notes.append(f"{ref}: value {comp.get('value')!r} not understood; left out")
                continue
            lines.append(f"{kind}{ref} {_node(a, ground)} {_node(b, ground)} {v:.6g}")
            edges.append((a, b))
        elif any(name.startswith(s) for s in SHORT) and len(pn) == 2:
            a, b = list(pn.values())
            lines.append(f"R{ref} {_node(a, ground)} {_node(b, ground)} 0.05")
            edges.append((a, b))
        else:
            model = find_model(comp, models)
            f = facts.get(mpn_of(comp))
            if f:
                model = {"type": "opamp", **(model or {}), **{k: v for k, v in f.items() if k != "source"}}
                if f.get("gbw_hz"):
                    notes.append(f"{ref}: GBW {f['gbw_hz'] / 1e6:g} MHz from {f['source']}")
            if model and not model.get("gbw_hz"):
                notes.append(f"{ref}: no GBW (not in the part-fact cache or [review.models])")
                model = None
            if not model:
                touches = set(pn.values()) - rails - ground
                if touches:
                    (unmodelled if comp.get("pins") and len(pn) > 2 else omitted).append(ref)
                continue
            kind = model.get("type", "opamp")
            roles = _roles(comp, pins_of.get(lib_id, {}), model)
            need = FDA_PINS if kind == "fda" else OPAMP_PINS
            missing = [r for r in need if r not in roles or roles[r] not in pn]
            if missing:
                notes.append(f"{ref}: no pin for {missing} (set [review.models] pins); left open")
                unmodelled.append(ref)
                continue
            gbw, aol = float(model["gbw_hz"]), float(model.get("aol_db", 100))
            sub = re.sub(r"[^A-Za-z0-9_]", "_", f"{kind}_{comp.get('value', ref)}")
            if sub not in subckts:
                if kind == "fda":
                    subckts[sub] = _fda_subckt(sub, gbw, aol)
                else:
                    gen = gen or _kh_generator()
                    text = gen.generate_opamp_model(sub, {"gbw_hz": gbw, "aol_db": aol,
                                                          "slew_vus": model.get("slew_vus", 100),
                                                          "rro": model.get("rro", True)})
                    subckts[sub] = text.replace(f"OPAMP_{gen.sanitize_mpn(sub)}", sub)
            nodes = " ".join(_node(pn[roles[r]], ground) for r in need)
            lines.append(f"X{ref} {nodes} {sub}")
            sig = [pn[roles[r]] for r in need if r not in ("vcc", "vee")]
            edges += [(a, b) for a in sig for b in sig if a < b]
    # Reachability through modelled parts, not through rails or ground.
    adj = {}
    for a, b in edges:
        if a in rails or b in rails or a in ground or b in ground:
            continue
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)
    seen, todo = {c["in"]}, [c["in"]]
    while todo:
        for m in adj.get(todo.pop(), ()):
            if m not in seen:
                seen.add(m)
                todo.append(m)
    if not all(o in seen for o in outs):
        near = sorted(r for r in unmodelled)
        return None, notes, (f"no path from {c['in']} to {outs} through modelled parts"
                             + (f"; unmodelled ICs: {near}" if near else ""))
    srcs, unknown = [], []
    for n in sorted(rails):
        v = rail_volts(n, dc)
        if v is None:
            unknown.append(n)
            v = 0.0
        srcs.append(f"V_{_node(n, ground)[2:]} {_node(n, ground)} 0 DC {v:g}")
    inn = _node(c["in"], ground)
    if source_ohms:
        srcs.append(f"VSIG n_sig_src 0 DC 0 AC 1\nRSIG n_sig_src {inn} {source_ohms:g}")
    else:
        srcs.append(f"VSIG {inn} 0 DC 0 AC 1")
    on_path = {comp["ref"] for comp in netlist["components"]
               if any(p.get("net") in seen for p in comp["pins"])}
    path_rails = {p.get("net") for comp in netlist["components"] if comp["ref"] in on_path
                  for p in comp["pins"]}
    notes = [n for n in notes if n.split(":")[0] in on_path]  # per-part notes, this path only
    for n in unknown:
        if n in path_rails:
            notes.append(f"rail {n}: voltage unknown, held at 0 V (set [review] dc)")
    if set(omitted) & on_path:
        notes.append(f"left out (no small-signal model): {sorted(set(omitted) & on_path)}")
    if set(unmodelled) & on_path:
        notes.append(f"open circuits on the path (no model): {sorted(set(unmodelled) & on_path)}")
    out_v = (f"v({_node(outs[0], ground)})-v({_node(outs[1], ground)})" if len(outs) == 2
             else f"v({_node(outs[0], ground)})")
    f_hi = max(float(c["at_hz"]) * 10, 1e6)
    meas = [f"meas ac g_at find gdb at={float(c['at_hz']):g}"]
    if c.get("ref_hz"):
        meas.append(f"meas ac g_ref find gdb at={float(c['ref_hz']):g}")
    text = "\n".join([f"* boardsmith SPICE path gate: {c['id']}", ".option rshunt=1e9",
                      *subckts.values(), *lines, *srcs, ".control",
                      f"ac dec 200 1 {f_hi:g}", f"let gdb = db({out_v})", *meas, ".endc", ".end", ""])
    return text, notes, None


def find_ngspice(path=""):
    return path or shutil.which("ngspice")


def run(text, ngspice, workdir=None, timeout=60):
    """{g_at, g_ref} in dB from ngspice batch mode, or {"error": ...}."""
    workdir = workdir or tempfile.mkdtemp(prefix="bs_spice_")
    os.makedirs(workdir, exist_ok=True)
    cir = os.path.join(workdir, "path.cir")
    with open(cir, "w") as f:
        f.write(text)
    try:
        p = subprocess.run([ngspice, "-b", cir], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"error": f"ngspice timed out after {timeout} s", "cir": cir}
    out = {"cir": cir}
    for m in re.finditer(r"^(g_at|g_ref)\s*=\s*([-+0-9.eE]+)", p.stdout, re.M):
        out[m.group(1)] = float(m.group(2))
    if "g_at" not in out:
        tail = (p.stdout + p.stderr).strip().splitlines()[-8:]
        out["error"] = "ngspice gave no result: " + " | ".join(tail)
    return out


def check(netlist, c, models, ngspice, dc=None, workdir=None, facts=None):
    """Result for one ac_gain constraint: status PASS/FAIL/NOT CHECKED, gain_db, notes."""
    text, notes, problem = testbench(netlist, c, models, dc, c.get("source_ohms", 0.0), facts)
    res = {"id": c["id"], "kind": c["kind"], "tag": c["tag"], "notes": notes}
    if problem:
        return {**res, "status": "NOT CHECKED", "why": problem}
    if not ngspice:
        return {**res, "status": "NOT CHECKED", "why": "ngspice not found"}
    r = run(text, ngspice, workdir)
    if "error" in r:
        return {**res, "status": "NOT CHECKED", "why": r["error"], "cir": r.get("cir")}
    g = r["g_at"] - r.get("g_ref", 0.0)
    ok = (("min_db" not in c or g >= c["min_db"]) and ("max_db" not in c or g <= c["max_db"]))
    res.update({"status": "PASS" if ok else "FAIL", "gain_db": round(g, 2),
                "at_hz": c["at_hz"], "cir": r["cir"]})
    if c.get("ref_hz"):
        res["ref_db"] = round(r["g_ref"], 2)
    res["limit"] = {k: c[k] for k in ("min_db", "max_db") if k in c}
    return res
