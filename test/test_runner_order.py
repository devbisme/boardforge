"""netlist.json is written in a stable order, whatever order SKiDL builds it in."""

import importlib.util

import pytest


def _runner():
    if importlib.util.find_spec("skidl") is None:
        pytest.skip("skidl not installed")
    from boardsmith import skidl_runner
    return skidl_runner


def test_stable_order():
    sr = _runner()
    a = {"sheets": [["", "b"], ["", "a"]],
         "components": [{"ref": "U1", "pins": [{"num": "10", "unit": 1}, {"num": "2", "unit": 1},
                                               {"num": "EP", "unit": 1}, {"num": "1", "unit": 2}]}]}
    sr.stable_order(a)
    assert a["sheets"] == [["", "a"], ["", "b"]]
    assert [p["num"] for p in a["components"][0]["pins"]] == ["2", "10", "EP", "1"]


def test_strip_zones():
    from boardsmith.render import strip_zones
    t = '(kicad_pcb\n\t(net 1 "GND")\n\t(zone (net 1) (polygon (pts (xy 0 0))))\n\t(footprint "x")\n)'
    out = strip_zones(t)
    assert "(zone" not in out and '(footprint "x")' in out and out.count("(") == out.count(")")
