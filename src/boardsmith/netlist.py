"""Netlists: read KiCad's kicadsexpr netlist and SKiDL's generic JSON; compare them.

Pure Python (no pcbnew), so both the orchestrator and the KiCad-Python board scripts
import it.
"""

import json
import re


def sexp(text):
    """Parse s-expression text into nested lists of strings."""
    tokens = re.findall(r'\(|\)|"(?:\\.|[^"\\])*"|[^\s()]+', text)
    stack, cur = [], []
    for t in tokens:
        if t == "(":
            stack.append(cur)
            cur = []
        elif t == ")":
            done = cur
            cur = stack.pop()
            cur.append(done)
        else:
            cur.append(t[1:-1].replace('\\"', '"').replace("\\\\", "\\") if t.startswith('"') else t)
    return cur[0]


def _find(node, key):
    return [n for n in node if isinstance(n, list) and n and n[0] == key]


def _val(node, key, default=None):
    f = _find(node, key)
    return f[0][1] if f and len(f[0]) > 1 else default


def read_kicad_netlist(path):
    """Return (components, nets) from a kicad-cli ``kicadsexpr`` netlist.

    components: {ref: {ref, value, footprint, sheetnames, sheettstamps, tstamp, fields}}
    nets: {name: [(ref, pin), ...]}
    """
    with open(path) as f:
        root = sexp(f.read())
    comps = {}
    for c in _find(_find(root, "components")[0], "comp"):
        ref = _val(c, "ref")
        sp = _find(c, "sheetpath")[0]
        fields = {}
        for fl in _find(c, "fields"):
            for f in _find(fl, "field"):
                fields[_val(f, "name")] = f[2] if len(f) > 2 else ""
        comps[ref] = dict(
            ref=ref, value=_val(c, "value", ""), footprint=_val(c, "footprint", ""),
            sheetnames=_val(sp, "names"), sheettstamps=_val(sp, "tstamps"),
            tstamp=_val(c, "tstamps") or "", fields=fields,
        )
    nets = {}
    for n in _find(_find(root, "nets")[0], "net"):
        nets[_val(n, "name")] = [(_val(nd, "ref"), _val(nd, "pin")) for nd in _find(n, "node")]
    return comps, nets


def read_generic(path):
    with open(path) as f:
        return json.load(f)


def _real(nodes):
    """Drop '#' references (power symbols, PWR_FLAGs): KiCad leaves them out of netlists."""
    return frozenset((r, str(p)) for r, p in nodes if not r.startswith("#"))


def compare(doc, kicad_nets):
    """Pin-for-pin comparison of the SKiDL document's nets with KiCad's.

    Returns a dict: ok, counts, missing (SKiDL nets with no identical KiCad net), extra
    (KiCad nets with no identical SKiDL net), and net_map {skidl name: kicad name}.
    """
    want = {}
    for n in doc["nets"]:
        if n.get("nc"):
            continue
        nodes = _real((nd["ref"], nd["pin"]) for nd in n["nodes"])
        if nodes:
            want[n["name"]] = nodes
    got = {}
    for name, nodes in kicad_nets.items():
        s = _real(nodes)
        # KiCad lists every unconnected pin as its own one-node net.
        if not s or (name.startswith("unconnected-") and len(s) == 1):
            continue
        got[name] = s
    by_nodes = {v: k for k, v in got.items()}
    net_map, missing = {}, []
    for name, nodes in want.items():
        k = by_nodes.get(nodes)
        if k is None:
            missing.append(name)
        else:
            net_map[name] = k
    matched = set(net_map.values())
    extra = sorted(k for k in got if k not in matched)
    pin_kicad = {p: k for k, v in got.items() for p in v}
    details = []
    for name in sorted(missing)[:20]:
        where = sorted({pin_kicad.get(p, "<none>") for p in want[name]})
        details.append(f"{name} ({len(want[name])} pins) -> KiCad {where[:5]}")
    return {
        "ok": not missing and not extra,
        "skidl_nets": len(want),
        "kicad_nets": len(got),
        "matched": len(net_map),
        "missing": sorted(missing),
        "extra": extra,
        "details": details,
        "net_map": net_map,
    }
