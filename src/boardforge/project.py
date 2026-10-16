"""KiCad project files that boardforge owns: .kicad_pro net classes and the lib tables.

pcbnew's ``board.Save()`` rewrites the .kicad_pro without net classes, so
``write_netclasses`` is idempotent and is re-applied after every board step.
"""

import json
import os

# SKiDL NetClass attribute spellings accepted besides KiCad's own keys.
_ALIASES = {"trace_width": "track_width", "width": "track_width", "via_dia": "via_diameter",
            "via_size": "via_diameter", "diff_pair_spacing": "diff_pair_gap"}
_KICAD_KEYS = {"clearance", "track_width", "via_diameter", "via_drill", "microvia_diameter",
               "microvia_drill", "diff_pair_width", "diff_pair_gap", "diff_pair_via_gap"}


def default_class(rules):
    return {
        "name": "Default",
        "clearance": rules["clearance"],
        "track_width": rules["track_width"],
        "via_diameter": rules["via_diameter"],
        "via_drill": rules["via_drill"],
        "microvia_diameter": 0.3,
        "microvia_drill": 0.1,
        "diff_pair_width": rules.get("diff_pair_width", 0.2),
        "diff_pair_gap": rules.get("diff_pair_gap", 0.25),
        "diff_pair_via_gap": 0.25,
        "bus_width": 12,
        "wire_width": 6,
        "line_style": 0,
        "pcb_color": "rgba(0, 0, 0, 0.000)",
        "schematic_color": "rgba(0, 0, 0, 0.000)",
        "priority": 2147483647,
        "tuning_profile": "",
    }


def netclasses_from_doc(doc, net_map, rules):
    """Net classes and per-net assignments (KiCad net names) from SKiDL's netlist JSON.

    Returns (classes, assignments, warnings). ``assignments`` is {kicad_net: [class, ...]}.
    """
    warnings = []
    base = default_class(rules)
    classes = []
    # SKiDL: a larger priority wins (default 0). KiCad: a smaller priority wins.
    defs = doc.get("netclasses", {})
    order = sorted(defs, key=lambda n: (-float(defs[n].get("priority", 0) or 0), n))
    for name in order:
        c = dict(base, name=name, priority=order.index(name))
        for k, v in defs[name].items():
            k = _ALIASES.get(k, k)
            if k in _KICAD_KEYS:
                c[k] = float(v)
            elif k != "priority":
                warnings.append(f"net class {name}: attribute {k!r} ignored")
        classes.append(c)
    assign = {}
    for n in doc["nets"]:
        ncs = n.get("attrs", {}).get("netclasses", [])
        if not ncs or n.get("nc"):
            continue
        k = net_map.get(n["name"])
        if k is None:
            warnings.append(f"net {n['name']} (class {ncs}) has no KiCad net")
            continue
        assign[k] = list(ncs)
    return classes, assign, warnings


def write_netclasses(pro_path, classes, assign, rules):
    """Put the net classes into a .kicad_pro, creating a minimal one if needed."""
    if os.path.exists(pro_path):
        with open(pro_path) as f:
            d = json.load(f)
    else:
        d = {"meta": {"filename": os.path.basename(pro_path), "version": 3}}
    ns = d.setdefault("net_settings", {})
    ns["classes"] = [default_class(rules)] + classes
    ns.setdefault("meta", {"version": 5})
    # Exact-name patterns: one per (net, class).
    ns["netclass_patterns"] = [{"netclass": c, "pattern": net}
                               for net, cs in sorted(assign.items()) for c in cs]
    ns["netclass_assignments"] = None
    with open(pro_path, "w") as f:
        json.dump(d, f, indent=2)


def footprint_paths(fp_libs, footprint_dir, overrides):
    """{nickname: .pretty path}: project overrides first, then KiCad's stock libraries."""
    return {n: os.path.abspath(os.path.expanduser(overrides.get(n) or
                                                  os.path.join(footprint_dir, n + ".pretty")))
            for n in sorted(set(fp_libs))}


def write_lib_tables(kicad_dir, sym_libs, fp_paths, symbol_dir):
    """Project-local sym-lib-table and fp-lib-table (a stale global table can't interfere).

    Returns the footprint library nicknames whose directory doesn't exist.
    """
    syms = dict(sym_libs)
    power = os.path.join(symbol_dir, "power.kicad_symdir")
    if "power" not in syms and os.path.exists(power):
        syms["power"] = power  # schematizer's power symbols and PWR_FLAGs
    with open(os.path.join(kicad_dir, "sym-lib-table"), "w") as f:
        f.write("(sym_lib_table\n  (version 7)\n")
        for nick, path in sorted(syms.items()):
            f.write(f'  (lib (name "{nick}") (type "KiCad") (uri "{path}") (options "") (descr ""))\n')
        f.write(")\n")
    with open(os.path.join(kicad_dir, "fp-lib-table"), "w") as f:
        f.write("(fp_lib_table\n  (version 7)\n")
        for nick, path in sorted(fp_paths.items()):
            f.write(f'  (lib (name "{nick}") (type "KiCad") (uri "{path}") (options "") (descr ""))\n')
        f.write(")\n")
    return [n for n, p in fp_paths.items() if not os.path.isdir(p)]
