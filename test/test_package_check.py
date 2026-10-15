"""Package check (§4.10 gate 5): pads against symbol pins and the datasheet package."""

from boardsmith.review import package_check as pc

# SOT-23-5-like: pads 1-3 on one side at 0.95 mm, 4-5 on the other; plus a QFN-ish variant
MOD = """(footprint "X"
  (fp_line (start -0.8 -1.45) (end 0.8 -1.45) (layer "F.Fab"))
  (fp_line (start 0.8 -1.45) (end 0.8 1.45) (layer "F.Fab"))
  (fp_line (start -0.8 1.45) (end -0.8 -1.45) (layer "F.Fab"))
  (fp_line (start -2 -2) (end 2 2) (layer "F.CrtYd"))
{pads})"""


def _pad(num, x, y, w=0.6, h=1.1):
    return f'  (pad "{num}" smd roundrect (at {x} {y}) (size {w} {h}) (layers "F.Cu"))\n'


SOT5 = (_pad(1, -1.1, -0.95) + _pad(2, -1.1, 0) + _pad(3, -1.1, 0.95)
        + _pad(4, 1.1, 0.95) + _pad(5, 1.1, -0.95))


def _libs(tmp_path, **mods):
    d = tmp_path / "L.pretty"
    d.mkdir(exist_ok=True)
    for name, pads in mods.items():
        (d / f"{name}.kicad_mod").write_text(MOD.format(pads=pads))
    return {"L": str(d)}


def _ex(n=5, pitch=0.95, thermal=False, body=None, pins=5, quality=80):
    return {"extraction": {"quality_score": quality},
            "base": {"package": {"code": "SOT-23-5", "pin_count": n, "pitch_mm": pitch,
                                 "thermal_pad": thermal, "body_mm": body},
                     "pinout": [{"numbers": [i], "name": f"P{i}", "type": "passive"}
                                for i in range(1, pins + 1)]}}


def _sev(findings):
    return sorted((f["severity"], f["check"]) for f in findings)


def test_footprint_geometry(tmp_path):
    libs = _libs(tmp_path, S5=SOT5, EP=SOT5 + _pad(6, 0, 0, 1.5, 2.0) + _pad(6, 0, 0.5, 0.3, 0.3))
    fp = pc.load_footprint("L:S5", libs)
    assert len(fp["pads"]) == 5 and fp["fab"] == (1.6, 2.9)
    assert pc.exposed(fp["pads"]) == set() and pc.pitch(fp["pads"], set()) == 0.95
    ep = pc.load_footprint("L:EP", libs)
    assert pc.exposed(ep["pads"]) == {"6"}
    assert pc.load_footprint("L:nope", libs) is None and pc.load_footprint("Z:S5", libs) is None


def test_package_against_extraction(tmp_path):
    libs = _libs(tmp_path, S5=SOT5, EP=SOT5 + _pad(6, 0, 0, 1.5, 2.0))
    fp, ep = pc.load_footprint("L:S5", libs), pc.load_footprint("L:EP", libs)
    assert _sev(pc.check_package("U1", "L:S5", fp, _ex())) == [("info", "package")]
    # exposed pad counted or not: both match
    assert _sev(pc.check_package("U1", "L:EP", ep, _ex(n=5, thermal=True))) == [("info", "package")]
    assert _sev(pc.check_package("U1", "L:EP", ep, _ex(n=6, thermal=True))) == [("info", "package")]
    bad = pc.check_package("U1", "L:S5", fp, _ex(n=8, pitch=1.27, thermal=True, pins=8))
    assert _sev(bad) == [("error", c) for c in ("pin_count", "pin_numbers", "pitch", "thermal_pad")]
    assert "['6', '7', '8']" in next(f["detail"] for f in bad if f["check"] == "pin_numbers")
    assert _sev(pc.check_package("U1", "L:EP", ep, _ex())) == [("warning", "thermal_pad")]
    # depopulated package: pads at twice the pin pitch
    assert _sev(pc.check_package("U1", "L:S5", fp, _ex(pitch=0.475))) == [("info", "package")]
    body = pc.check_package("U1", "L:S5", fp, _ex(body={"length": 5.3, "width": 5.3}))
    assert _sev(body) == [("warning", "body")]
    assert _sev(pc.check_package("U1", "L:S5", fp, _ex(body={"length": 3.0, "width": 1.7}))) == [("info", "package")]
    # a datasheet that calls the short side "length" still matches (CDCLVC1102 TSSOP-8)
    assert _sev(pc.check_package("U1", "L:S5", fp, _ex(body={"length": 1.7, "width": 3.0}))) == [("info", "package")]
    # an untrusted extraction can't fail the gate
    assert _sev(pc.check_package("U1", "L:S5", fp, _ex(n=8, quality=40))) == [("warning", "pin_count")]


def test_gate_results(tmp_path):
    libs = _libs(tmp_path, S5=SOT5)

    class Store:
        def extraction(self, mpn):
            return _ex() if mpn == "REG1" else None

    def comp(ref, mpn, fp, pins):
        return {"ref": ref, "footprint": fp, "fields": {"MPN": mpn},
                "pins": [{"num": n, "net": net} for n, net in pins.items()]}

    nl = {"components": [
        comp("U1", "REG1", "L:S5", {"1": "VIN", "2": "GND", "5": "OUT"}),
        comp("U2", "OTHER", "L:S5", {"1": "A", "6": "B", "7": "__NOCONNECT"}),
        comp("U4", "OTHER", "L:S5", {"1": "A", "2": "B", "3": "C", "4": "D"}),  # pad 5 has no pin
        comp("U3", "OTHER", "L:missing", {"1": "A"}),
        comp("TP1", "", "", {"1": "A"})]}
    res = {r["id"]: r for r in pc.check(nl, Store(), libs)}
    pins = res["PKG:pins"]
    assert pins["status"] == "FAIL" and pins["tag"] == "HARD" and pins["refs"] == ["U2"]
    # pads with no symbol pin: U1 (3, 4), U2 (2-5), U4 (5)
    assert _sev(pins["findings"]) == [("error", "pins")] + [("warning", "pads")] * 3 + [("warning", "pins")]
    assert any("U4" in f["detail"] and "pads ['5']" in f["detail"] for f in pins["findings"])
    err = next(f for f in pins["findings"] if f["severity"] == "error")
    assert "['6']" in err["detail"] and pins["why"] == "1 of 2 footprints found"
    assert res["PKG:REG1"]["status"] == "PASS" and "PKG:OTHER" not in res
    assert pc.check(nl, Store(), {})[0]["status"] == "NOT CHECKED"


def test_pad_gap(tmp_path):
    # SOT5's pads 1-3 overlap (deliberate in this fixture: skipped); pads 4-5 are 0.8 mm apart.
    # QFN: pads 1-2 are 0.2 apart, the exposed pad 3 sits 0.1 below pad 1.
    libs = _libs(tmp_path, S5=SOT5, QFN=_pad(1, 0, 0, 0.2, 0.8) + _pad(2, 0.4, 0, 0.2, 0.8)
                 + _pad(3, 0.2, 2, 3, 3) + _pad(3, 0.2, 2.5, 0.5, 0.5))
    assert abs(pc.pad_gap(pc.load_footprint("L:S5", libs)) - 0.8) < 1e-9
    assert abs(pc.pad_gap(pc.load_footprint("L:QFN", libs)) - 0.1) < 1e-9
