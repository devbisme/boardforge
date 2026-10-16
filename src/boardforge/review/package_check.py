"""Package check (§4.10 gate 5): footprints against symbols and datasheet packages.

- pins (every part): a symbol pin on a net with no footprint pad of the same number is an
  error (the USB-C SH/S1 and mirrored-connector class); KiCad would silently drop the
  connection. A numbered pad with no symbol pin is a warning: it gets no net (an exposed
  pad left floating).
- package (parts with an extraction): footprint pad count against ``base.package.pin_count``
  (with or without the exposed pad; extractions differ on counting it), pitch against
  ``pitch_mm``, a thermal pad against ``thermal_pad``, the datasheet pin numbers against the
  pads (errors), and the F.Fab body outline against ``body_mm`` (warning).

Footprints come from the fp-lib-table C1 wrote (the libraries the design was built with), else
from KiCad's stock library directory and [libraries] footprints.
"""

import math
import os
from collections import Counter

from boardforge import datasheets
from boardforge.netlist import _find, _val, sexp
from boardforge.review.datasheet_check import result, trust

PITCH_TOL = 0.02  # mm
BODY_TOL = (0.25, 0.10)  # mm, fraction: the larger applies
EP_AREA = 2.5  # a pad this many times the median pad area is an exposed pad


def _nat(num):
    return (0, int(num), "") if num.isdigit() else (1, 0, num)


def lib_paths(proj):
    """{nickname: .pretty dir}, or {} when no library location is known."""
    table = os.path.join(proj.out, "kicad", "fp-lib-table")
    if os.path.exists(table):
        with open(table) as f:
            return {_val(lib, "name"): _val(lib, "uri") for lib in _find(sexp(f.read()), "lib")}
    from boardforge.kicad import Kicad, KicadError

    try:
        fp_dir = Kicad.find(proj.cfg["kicad"]).footprint_dir
    except KicadError:
        fp_dir = ""
    over = proj.cfg["libraries"]["footprints"]  # absolute (config.Project.load)
    stock = {n[:-len(".pretty")]: os.path.join(fp_dir, n) for n in os.listdir(fp_dir)
             if n.endswith(".pretty")} if fp_dir else {}
    return stock | over


def load_footprint(lib_id, libs):
    """Pads and F.Fab extent of ``lib:name``, or None if the file isn't found.

    {"pads": [(number, x, y, area)], "rects": [(number, x0, y0, x1, y1)] of copper pads,
     "fab": (w, h) or None}
    """
    lib, _, name = lib_id.partition(":")
    path = os.path.join(libs.get(lib, ""), name + ".kicad_mod")
    if not libs.get(lib) or not os.path.exists(path):
        return None
    with open(path) as f:
        root = sexp(f.read())
    pads, rects = [], []
    for p in _find(root, "pad"):
        if len(p) < 3 or p[2] == "np_thru_hole" or not p[1]:
            continue
        at, size = _find(p, "at")[0], _find(p, "size")
        w, h = (float(size[0][1]), float(size[0][2])) if size else (0.0, 0.0)
        x, y = float(at[1]), float(at[2])
        pads.append((p[1], x, y, w * h))
        layers = " ".join(str(v) for lay in _find(p, "layers") for v in lay[1:])
        if "Cu" in layers:
            a = math.radians(float(at[3])) if len(at) > 3 else 0.0
            hw = (abs(w * math.cos(a)) + abs(h * math.sin(a))) / 2
            hh = (abs(w * math.sin(a)) + abs(h * math.cos(a))) / 2
            rects.append((p[1], x - hw, y - hh, x + hw, y + hh))
    xs, ys = [], []
    for kind in ("fp_line", "fp_rect", "fp_poly"):
        for g in _find(root, kind):
            if _val(g, "layer") != "F.Fab":
                continue
            pts = [n for k in ("start", "end") for n in _find(g, k)]
            pts += [n for pl in _find(g, "pts") for n in _find(pl, "xy")]
            xs += [float(n[1]) for n in pts]
            ys += [float(n[2]) for n in pts]
    fab = (round(max(xs) - min(xs), 3), round(max(ys) - min(ys), 3)) if xs else None
    return {"pads": pads, "rects": rects, "fab": fab}


def pad_gap(fp):
    """Smallest copper gap (mm) between two differently numbered pads of a footprint
    (bounding boxes). Pads that touch or overlap (a split exposed pad, USB-C's paired
    GND/VBUS pads) are deliberate and don't count."""
    best = math.inf
    rects = fp.get("rects", [])
    for i, (na, ax0, ay0, ax1, ay1) in enumerate(rects):
        for nb, bx0, by0, bx1, by1 in rects[i + 1:]:
            if na == nb:
                continue
            gap = math.hypot(max(0, bx0 - ax1, ax0 - bx1), max(0, by0 - ay1, ay0 - by1))
            if gap > 1e-6:
                best = min(best, gap)
    return best


def exposed(pads):
    """Pad numbers that are exposed/thermal pads: much larger than the typical pad."""
    area = {}
    for num, _, _, a in pads:
        area[num] = max(area.get(num, 0.0), a)
    if len(area) < 3:
        return set()
    med = sorted(area.values())[len(area) // 2]
    return {n for n, a in area.items() if a > EP_AREA * med or n.upper() in ("EP", "PAD")}


def pitch(pads, skip):
    """The commonest nearest-neighbour spacing between pads of different numbers (mm)."""
    pts = [(n, x, y) for n, x, y, _ in pads if n not in skip]
    near = []
    for n, x, y in pts:
        d = [((x - x2) ** 2 + (y - y2) ** 2) ** 0.5 for n2, x2, y2 in pts if n2 != n]
        if d:
            near.append(round(min(d), 2))
    if not near:
        return None
    counts = Counter(near).most_common()
    return min(v for v, c in counts if c == counts[0][1])


def axis_pitches(pads, skip):
    """The smallest spacing (mm) between pads in the same row and in the same column."""
    pts = [(x, y) for n, x, y, _ in pads if n not in skip]
    out = set()
    for axis in (0, 1):
        d = [abs(a[axis] - b[axis]) for i, a in enumerate(pts) for b in pts[i + 1:]
             if abs(a[1 - axis] - b[1 - axis]) < 0.01 and abs(a[axis] - b[axis]) > 0.01]
        if d:
            out.add(round(min(d), 2))
    return out


def check_pins(comp, fp):
    """An error for symbol pins on nets with no footprint pad; a warning for numbered pads
    with no symbol pin (no net on the board: an exposed pad left floating, say)."""
    nums = {p[0] for p in fp["pads"]}
    pins = {p["num"] for p in comp.get("pins", [])}
    bad = [p["num"] for p in comp.get("pins", [])
           if p.get("net") and "NOCONNECT" not in p["net"].upper() and p["num"] not in nums]
    out = [{"severity": "error", "check": "pins",
            "detail": f"{comp['ref']} ({comp['footprint']}): symbol pins {bad} on nets have no pad"}] if bad else []
    loose = sorted(nums - pins, key=_nat)
    if loose and pins:
        out.append({"severity": "warning", "check": "pads",
                    "detail": f"{comp['ref']} ({comp['footprint']}): pads {loose} have no symbol pin, so no net"})
    return out


def check_package(ref, lib_id, fp, ex):
    """Findings for one footprint against the extraction's package and pinout."""
    base = ex.get("base") or {}
    pkg = base.get("package") or {}
    out = []
    nums = {p[0] for p in fp["pads"]}
    ep = exposed(fp["pads"])
    code = pkg.get("code", "?")
    n = pkg.get("pin_count")
    if n is not None and n not in (len(nums), len(nums - ep)):
        out.append({"severity": "error", "check": "pin_count",
                    "detail": f"{ref} {lib_id}: {len(nums)} pads ({len(ep)} exposed), datasheet {code} has {n} pins"})
    want = pkg.get("pitch_mm")
    have = pitch(fp["pads"], ep)
    # A package can have a pitch along each axis (a 4-pad 3.2 x 2.5 mm oscillator: 2.1 and
    # 1.65 mm), and a depopulated one (SOT-23-3) pads at twice the pin pitch: any fits.
    cands = {have} | axis_pitches(fp["pads"], ep) if have is not None else set()
    if want and cands and min(abs(k * want - c) for k in (1, 2) for c in cands) > PITCH_TOL:
        out.append({"severity": "error", "check": "pitch",
                    "detail": f"{ref} {lib_id}: pad pitch {have:g} mm, datasheet {code} {want:g} mm"})
    tp = pkg.get("thermal_pad")
    if tp is True and not ep:
        out.append({"severity": "error", "check": "thermal_pad",
                    "detail": f"{ref} {lib_id}: datasheet {code} has a thermal pad, the footprint has none"})
    elif tp is False and ep:
        out.append({"severity": "warning", "check": "thermal_pad",
                    "detail": f"{ref} {lib_id}: footprint has exposed pad(s) {sorted(ep)}, datasheet {code} none"})
    pin_nums = {str(x) for p in base.get("pinout") or [] for x in p.get("numbers") or []}
    if pin_nums - nums:
        out.append({"severity": "error", "check": "pin_numbers",
                    "detail": f"{ref} {lib_id}: datasheet pins {sorted(pin_nums - nums, key=_nat)} have no pad"})
    body = pkg.get("body_mm") or {}
    if fp["fab"] and body.get("length") and body.get("width"):
        # datasheets differ on which side is "length": compare long side with long side
        have_b = sorted(fp["fab"], reverse=True)
        want_b = sorted((body["length"], body["width"]), reverse=True)
        if any(abs(h - w) > max(BODY_TOL[0], BODY_TOL[1] * w) for h, w in zip(have_b, want_b)):
            out.append({"severity": "warning", "check": "body",
                        "detail": f"{ref} {lib_id}: F.Fab outline {have_b[0]:g} x {have_b[1]:g} mm, "
                                  f"datasheet body {want_b[0]:g} x {want_b[1]:g} mm"})
    if not out:
        out.append({"severity": "info", "check": "package",
                    "detail": f"{ref} {lib_id}: {len(nums)} pads, pitch {have} mm match {code}"})
    return trust(out, ex)


def check(netlist, store, libs):
    """Gate results: PKG:pins for every footprinted part, PKG:<MPN> per extracted part."""
    cache, missing, pin_findings, bad_refs, results = {}, [], [], [], []

    def fp_of(lib_id):
        if lib_id not in cache:
            cache[lib_id] = load_footprint(lib_id, libs)
        return cache[lib_id]

    if not libs:
        return [{"id": "PKG:pins", "kind": "package", "tag": "HARD", "status": "NOT CHECKED",
                 "why": "no footprint libraries: run `boardforge sch` (C1) or install KiCad"}]
    by_mpn = {}
    for comp in netlist["components"]:
        if not comp.get("footprint"):
            continue
        fp = fp_of(comp["footprint"])
        if fp is None:
            missing.append(f"{comp['ref']} ({comp['footprint']})")
            continue
        f = check_pins(comp, fp)
        pin_findings += f
        bad_refs += [comp["ref"]] if any(x["severity"] == "error" for x in f) else []
        mpn = datasheets.mpn_of(comp)
        if mpn:
            by_mpn.setdefault(mpn, []).append(comp)
    if missing:
        pin_findings.append({"severity": "warning", "check": "pins",
                             "detail": f"footprints not found: {', '.join(missing)}"})
    results.append(result("PKG:pins", "package", bad_refs, pin_findings))
    results[-1]["why"] = f"{sum(v is not None for v in cache.values())} of {len(cache)} footprints found"
    for mpn, comps in sorted(by_mpn.items()):
        ex = store.extraction(mpn)
        if ex is None or not (ex.get("base") or {}).get("package"):
            continue
        seen, findings = set(), []
        for c in comps:
            if c["footprint"] not in seen:
                seen.add(c["footprint"])
                findings += check_package(c["ref"], c["footprint"], fp_of(c["footprint"]), ex)
        results.append(result(f"PKG:{mpn}", "package", [c["ref"] for c in comps], findings))
    return results
