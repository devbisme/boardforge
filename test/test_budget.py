"""Budget recheck (§4.10 gate 6): rail loads against the source regulator's rating."""

from boardsmith.review import budget


def _reg(iout, quality=80, field="max"):
    return {"extraction": {"quality_score": quality},
            "base": {"pinout": [{"numbers": ["1"], "name": "IN", "type": "power_in"},
                                {"numbers": ["3"], "name": "SW", "type": "power_out"},
                                {"numbers": ["5"], "name": "OUT", "type": "power_out"}]},
            "regulator": {"iout_max": [{"min": None, "typ": None, "max": None, field: iout, "unit": "A"}]}}


class Store:
    def __init__(self, **ex):
        self.ex = ex

    def extraction(self, mpn):
        return self.ex.get(mpn)


def _nl(*comps):
    """comps: (ref, mpn, {pin: (net, pintype)})."""
    cl = [{"ref": r, "fields": {"MPN": m} if m else {}, "pins": [{"num": p, "net": n} for p, (n, _) in pins.items()]}
          for r, m, pins in comps]
    nets = {}
    for r, _, pins in comps:
        for p, (n, t) in pins.items():
            nets.setdefault(n, []).append({"ref": r, "pin": p, "pintype": t})
    return {"components": cl, "nets": [{"name": n, "nodes": v} for n, v in nets.items()]}


C = {"id": "R", "kind": "rail_current", "tag": "HARD", "net": "+3V3", "max_a": 0.25}
LDO = ("U1", "LDO1", {"1": ("+5V", "POWER-IN"), "5": ("+3V3", "POWER-OUT")})
BUCK = [("U2", "BUCK1", {"1": ("+5V", "POWER-IN"), "3": ("SW", "POWER-OUT")}),
        ("L1", "", {"1": ("SW", "PASSIVE"), "2": ("+3V3", "PASSIVE")})]
FLAG = ("#FLG1", "", {"1": ("+3V3", "POWER-OUT")})


def test_direct_and_through_inductor():
    r = budget.check(_nl(LDO, FLAG), C, Store(LDO1=_reg(0.3)))
    assert r["status"] == "PASS" and r["refs"] == ["U1"] and "83 %" in r["findings"][0]["detail"]
    r = budget.check(_nl(*BUCK), C, Store(BUCK1=_reg(0.2)))
    assert r["status"] == "FAIL" and r["tag"] == "HARD" and "via L1" in r["findings"][0]["detail"]
    # a guaranteed-minimum rating is used when there is no max
    assert budget.check(_nl(LDO), C, Store(LDO1=_reg(0.6, field="min")))["status"] == "PASS"


def test_not_checked_and_trust():
    r = budget.check(_nl(LDO), C, Store())  # symbol says POWER-OUT, no extraction
    assert r["status"] == "NOT CHECKED" and "no extraction" in r["why"] and r["refs"] == ["U1"]
    assert "no power-output" in budget.check(_nl(FLAG), C, Store())["why"]
    assert "several" in budget.check(_nl(LDO, *BUCK), C, Store())["why"]
    assert "not in the netlist" in budget.check(_nl(LDO), dict(C, net="X"), Store())["why"]
    r = budget.check(_nl(LDO), C, Store(LDO1=_reg(0.1, quality=40)))
    assert r["status"] == "FAIL" and r["tag"] == "SOFT" and r["findings"][0]["severity"] == "warning"


def test_load_switch_rated_by_recommended_iout():
    sw = {"extraction": {"quality_score": 80},
          "base": {"pinout": [{"numbers": ["5"], "name": "OUT", "type": "power_out"}],
                   "recommended_operating": {"IOUT": [{"min": 0, "max": 1.2, "unit": "A"},
                                                      {"min": 0, "max": 1.5, "unit": "A"}]}}}
    r = budget.check(_nl(LDO), C, Store(LDO1=sw))
    assert r["status"] == "PASS" and "rated 1.2 A (recommended IOUT)" in r["findings"][0]["detail"]
