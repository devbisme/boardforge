"""B4 review gate: value parsing, constraints.json, SPICE path testbenches (ngspice)."""

import json

import pytest

from boardsmith.review import constraints as cons
from boardsmith.review import spice_path as sp
from boardsmith.units import si

NGSPICE = sp.find_ngspice()
needs_ngspice = pytest.mark.skipif(not NGSPICE, reason="ngspice not found")


def test_si_values():
    assert si("1k 0.1%", "R") == 1000
    assert si("4k7", "R") == pytest.approx(4700)
    assert si("15pF C0G", "F") == pytest.approx(15e-12)
    assert si("3-10pF", "F") == pytest.approx(6.5e-12)  # trimmer: midpoint
    assert si("49.9", "R") == pytest.approx(49.9)
    assert si("120R@100MHz", "R") == 120
    assert si("DNP", "R") is None


def test_constraints_validation(tmp_path):
    f = tmp_path / "constraints.json"
    f.write_text(json.dumps({"schema": "boardsmith-constraints", "version": 1, "constraints": [
        {"id": "A", "kind": "ac_gain", "in": "X", "out": "Y", "at_hz": 1e3, "max_db": 0},
        {"id": "A", "kind": "ac_gain", "tag": "MAYBE", "in": "X", "at_hz": 1e3},
        {"id": "C", "kind": "thd"}]}))
    cs, problems = cons.load(str(f))
    assert [c["id"] for c in cs] == ["A", "A", "C"] and cs[0]["tag"] == "HARD"
    assert cs[1].get("invalid") and not cs[2].get("invalid")  # unknown kinds are kept
    assert any("duplicate" in p for p in problems) and any("MAYBE" in p for p in problems)
    assert any("needs out, min_db or max_db" in p for p in problems)
    assert cons.load(str(tmp_path / "none.json")) == ([], [])


def _sym(name, pins):
    """An embedded symbol with pins [(number, name, type)]."""
    return {"draw_cmds": {"1": [["pin", t, "line", ["name", n], ["number", num]]
                                for num, n, t in pins]}}


def _part(ref, lib_id, value, nets):
    return {"ref": ref, "lib_id": lib_id, "value": value, "fields": {},
            "pins": [{"num": str(i + 1), "net": n} for i, n in enumerate(nets)]}


def _netlist(parts, symbols=None, power=()):
    nets = {}
    for p in parts:
        for pin in p["pins"]:
            nets.setdefault(pin["net"], []).append({"ref": p["ref"], "pin": pin["num"], "pintype":
                                                    "POWER-IN" if pin["net"] in power else "PASSIVE"})
    return {"symbols": symbols or {}, "components": parts,
            "nets": [{"name": n, "nodes": x} for n, x in nets.items()]}


@needs_ngspice
def test_rc_lowpass_corner(tmp_path):
    nl = _netlist([_part("R1", "Device:R", "1k", ["IN", "OUT"]),
                   _part("C1", "Device:C", "159.155nF", ["OUT", "GND"])])
    c = {"id": "rc", "kind": "ac_gain", "tag": "HARD", "in": "IN", "out": "OUT",
         "at_hz": 1e3, "ref_hz": 10, "max_db": -2.9}
    r = sp.check(nl, c, {}, NGSPICE, workdir=str(tmp_path))
    assert r["status"] == "PASS" and r["gain_db"] == pytest.approx(-3.01, abs=0.05)


@needs_ngspice
def test_opamp_follower_and_fda(tmp_path):
    opamp = _sym("OPA", [("1", "+", "input"), ("2", "-", "input"), ("3", "", "output"),
                         ("4", "V+", "power_in"), ("5", "V-", "power_in")])
    fda = _sym("FDA", [("1", "+", "input"), ("2", "-", "input"), ("3", "", "output"),
                       ("4", "", "output"), ("5", "V_{OCM}", "input"), ("6", "V_{S+}", "power_in"),
                       ("7", "V_{S-}", "power_in")])
    parts = [_part("U1", "lib:OPA", "OPAX", ["IN", "BUF", "BUF", "+5V", "-5V"]),
             # FDA with Rf = Rg = 1k: differential gain 1 from a single-ended input
             _part("U2", "lib:FDA", "FDAX", ["SP", "SN", "OP", "ON", "VCM", "+5V", "GND"]),
             _part("R1", "Device:R", "1k", ["BUF", "SP"]), _part("R2", "Device:R", "1k", ["GND", "SN"]),
             _part("R3", "Device:R", "1k", ["SP", "ON"]), _part("R4", "Device:R", "1k", ["SN", "OP"])]
    nl = _netlist(parts, {"lib:OPA": opamp, "lib:FDA": fda}, power=("+5V", "-5V", "GND"))
    models = {"^OPAX": {"type": "opamp", "gbw_hz": 100e6},
              "^FDAX": {"type": "fda", "gbw_hz": 100e6, "pins": {"outp": 3, "outn": 4}}}
    c = {"id": "f", "kind": "ac_gain", "tag": "HARD", "in": "IN", "out": ["OP", "ON"],
         "at_hz": 1e3, "min_db": -0.1, "max_db": 0.1}
    r = sp.check(nl, c, models, NGSPICE, dc={"VCM": 1.0}, workdir=str(tmp_path))
    assert r["status"] == "PASS", (r.get("why"), r.get("notes"))
    # without the FDA model the path crosses an unmodelled IC: not checked, and says why
    r = sp.check(nl, c, {"^OPAX": models["^OPAX"]}, NGSPICE, workdir=str(tmp_path))
    assert r["status"] == "NOT CHECKED" and "U2" in r["why"]


@needs_ngspice
def test_gate_status_and_report(tmp_path):
    from boardsmith.config import Project
    from boardsmith.review.gate import review

    (tmp_path / "d.py").write_text("")
    (tmp_path / "boardsmith.toml").write_text('[design]\nfile = "d.py"\n')
    proj = Project.load(str(tmp_path))
    nl = _netlist([_part("R1", "Device:R", "1k", ["IN", "OUT"]),
                   _part("C1", "Device:C", "159.155nF", ["OUT", "GND"])])
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "netlist.json").write_text(json.dumps(nl))
    base = {"kind": "ac_gain", "in": "IN", "out": "OUT", "at_hz": 1e3, "ref_hz": 10}

    def run(*cs):
        (tmp_path / "constraints.json").write_text(json.dumps(
            {"schema": "boardsmith-constraints", "version": 1, "constraints": list(cs)}))
        return review(proj)

    rep = run({**base, "id": "ok", "tag": "HARD", "max_db": -2.9},
              {**base, "id": "soft", "tag": "SOFT", "max_db": -10},
              {"id": "rail", "kind": "rail_current", "tag": "ASSUMED", "net": "+3V3", "max_a": 1})
    assert rep["status"] == "WARN" and rep["soft_fail"] == ["soft"] and rep["not_checked"] == ["rail"]
    assert (tmp_path / "build" / "reports" / "review.json").exists()
    rep = run({**base, "id": "hard", "tag": "HARD", "max_db": -10})
    assert rep["status"] == "FAIL" and rep["hard_fail"] == ["hard"]
