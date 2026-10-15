"""Datasheet check (§4.10 gate 4): supply spans, power pins, trust, gate status."""

import json
import os

from boardsmith.review import datasheet_check as dc


def _sv(mn=None, mx=None):
    return [{"min": mn, "typ": None, "max": mx, "unit": "V", "condition": None, "notes": None,
             "evidence": {"page": 1, "section": "x", "confidence": "high", "method": "table"}}]


def _fda(quality=80):
    """THS4521-like: VS 2.7..5.4 V recommended, 5.5 V absolute, VS+ = 3, VS- = 6."""
    pins = [("1", "VIN-", "input", None), ("3", "VS+", "power_in", "VS"), ("4", "VOUT+", "output", None),
            ("6", "VS-", "power_in", "VS"), ("8", "VIN+", "input", None)]
    return {"extraction": {"quality_score": quality},
            "base": {"absolute_max": {"VS_max": _sv(mx=5.5)}, "recommended_operating": {"VS": _sv(2.7, 5.4)},
                     "pinout": [{"numbers": [n], "name": nm, "type": t, "power_domain": d}
                                for n, nm, t, d in pins]}}


def _comp(nets):
    return {"ref": "U9", "pins": [{"num": n, "net": net} for n, net in nets.items()]}


RAILS = {"+3V3", "-3V3A", "+2V5"}
GROUND = {"GND"}


def _run(nets, ex=None):
    return dc.check_part(_comp(nets), ex or _fda(), RAILS, GROUND, {})


def _sev(findings):
    return [(f["severity"], f["check"]) for f in findings]


def test_supply_span():
    ok = _run({"3": "+3V3", "6": "GND", "1": "A", "8": "B", "4": "C"})
    assert _sev(ok) == [("info", "supply")] and "3.3 V within" in ok[0]["detail"]
    # split rails: the span is 6.6 V, over the 5.5 V absolute maximum
    over = _run({"3": "+3V3", "6": "-3V3A", "1": "A", "8": "B"})
    assert _sev(over) == [("error", "supply")] and "6.6 V" in over[0]["detail"]
    low = _run({"3": "+2V5", "6": "GND"})
    assert _sev(low) == [("warning", "supply")] and "outside recommended" in low[0]["detail"]


def test_power_pins_and_trust():
    f = _run({"3": "+3V3", "6": "__NOCONNECT"})
    assert ("error", "power_pin") in _sev(f)
    f = _run({"3": "+3V3", "6": "SOME_SIGNAL"})
    assert ("warning", "power_pin") in _sev(f)
    # an untrusted extraction can't fail the gate
    f = _run({"3": "+3V3", "6": "-3V3A"}, _fda(quality=40))
    assert _sev(f) == [("warning", "supply")] and "not trusted" in f[0]["detail"]


def test_single_supply_key_and_domains():
    ex = _fda()
    for p in ex["base"]["pinout"]:
        p["power_domain"] = None
    ex["base"]["recommended_operating"] = {"VDD": _sv(3.0, 3.6)}
    ex["base"]["absolute_max"] = {"VDD_max": _sv(mx=4.0)}
    assert _sev(_run({"3": "+3V3", "6": "GND"}, ex)) == [("info", "supply")]
    ex["base"]["recommended_operating"] = {"VA": _sv(3.0, 3.6), "VB": _sv(1.1, 1.3)}
    f = _run({"3": "+3V3", "6": "GND"}, ex)
    assert _sev(f) == [("info", "supply")] and "no supply key" in f[0]["detail"]


def test_gate_fails_on_datasheet_error(tmp_path):
    from boardsmith.config import Project
    from boardsmith.review.gate import review

    (tmp_path / "d.py").write_text("")
    (tmp_path / "boardsmith.toml").write_text('[design]\nfile = "d.py"\n[datasheets]\ncache = ""\n')
    proj = Project.load(str(tmp_path))
    nets = {"3": "+3V3", "6": "-3V3A", "1": "A", "4": "B", "8": "C"}
    comp = {"ref": "U9", "lib_id": "x:y", "value": "FDA", "fields": {"MPN": "FDA1"},
            "pins": [{"num": n, "net": v} for n, v in nets.items()]}
    nl = {"symbols": {}, "components": [comp, {"ref": "U1", "lib_id": "x:y", "value": "X",
                                               "fields": {"MPN": "NOFACTS"}, "pins": []}],
          "nets": [{"name": v, "nodes": [{"ref": "U9", "pin": n, "pintype":
                                          "POWER-IN" if n in "36" else "PASSIVE"}]} for n, v in nets.items()]}
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "netlist.json").write_text(json.dumps(nl))
    os.makedirs(tmp_path / "datasheets" / "extracted")
    (tmp_path / "datasheets" / "extracted" / "FDA1.json").write_text(json.dumps(_fda()))
    rep = review(proj)
    res = {r["id"]: r for r in rep["results"]}
    assert rep["status"] == "FAIL" and rep["hard_fail"] == ["DS:FDA1"]
    assert res["DS:coverage"]["status"] == "NOT CHECKED" and "NOFACTS" in res["DS:coverage"]["why"]
    assert any("stale" in f["detail"] for f in res["DS:FDA1"]["findings"])  # no PDF behind it
