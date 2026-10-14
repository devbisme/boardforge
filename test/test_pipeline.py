"""End-to-end: the tiny example through C1-C6 (needs KiCad 10 and Freerouting; ~1 min).

Run with ``pytest -m kicad``. Skipped when KiCad 10 isn't found.
"""

import json
import os
import re
import shutil

import pytest

from boardsmith import routers
from boardsmith.config import Project
from boardsmith.kicad import Kicad, KicadError

HERE = os.path.dirname(__file__)


def _kicad():
    try:
        k = Kicad.find({})
    except KicadError:
        return None
    return None if k.warnings else k


KICAD = _kicad()
pytestmark = [pytest.mark.kicad, pytest.mark.slow,
              pytest.mark.skipif(KICAD is None, reason="KiCad 10 not found")]


@pytest.fixture(scope="module")
def tiny(tmp_path_factory):
    from boardsmith.physical import Pipeline

    d = tmp_path_factory.mktemp("tiny")
    for f in ("tiny.py", "boardsmith.toml"):
        shutil.copy(os.path.join(HERE, "examples", "tiny", f), d)
    proj = Project.load(str(d))
    if not routers.get("freerouting").available(proj.cfg)[0]:
        # Without Freerouting the A* router does all the routing.
        proj = Project.load(str(d), overrides={"routing": {"chain": ["astar"]}})
    pipe = Pipeline(proj, KICAD)
    results = {r["stage"]: r for r in pipe.run()}
    return proj, results


def test_all_stages_ran(tiny):
    _, res = tiny
    assert set(res) == {"c1", "c2", "c3", "c4", "c5", "c6"}
    assert all(r["status"] in ("done", "warn") for r in res.values()), \
        {s: r["summary"] for s, r in res.items() if r["status"] == "failed"}


def test_placement_reports_congestion(tiny):
    proj, res = tiny
    s = res["c3"]["summary"]
    assert not s["unplaced"] and "overlaps" not in s
    c = s["congestion"]
    assert c["ratsnest_mm"] > 0 and 0 < c["max"] < 1.0  # a small, loose board
    assert os.path.exists(os.path.join(proj.out, "reports", "congestion.json"))


def test_compaction_shrinks_and_routes(tiny):
    proj, res = tiny
    comp = res["c3"]["summary"]["compaction"]
    with open(os.path.join(proj.out, "reports", "compaction.json")) as f:
        rep = json.load(f)
    assert rep["trials"][0]["scale"] == 1.0 and rep["trials"][0]["fits"]
    assert comp["scale"] == rep["chosen"] <= 1.0
    if comp["scale"] < 1.0:
        assert res["c2"]["summary"]["size"] == comp["size"]
    assert res["c4"]["summary"]["unrouted"] == 0  # confirmed by routing (or stepped back up)


def test_region_holds_its_part(tiny):
    proj, res = tiny
    assert not any("region" in w for w in res["c3"]["warnings"])
    with open(os.path.join(proj.out, "kicad", "tiny.kicad_pcb")) as f:
        pcb = f.read()
    xs = [float(x) for x in re.findall(r'\(gr_line\s+\(start ([\d.]+) [\d.]+\)', pcb)]
    u1 = re.search(r'\(footprint "Package_TO_SOT_SMD:SOT-23-5"\s+\(layer "F.Cu"\)\s+'
                   r'\(uuid "[^"]+"\)\s+\(at ([\d.]+) ', pcb)
    assert (min(xs) + max(xs)) / 2 < float(u1.group(1)) < max(xs)  # right half (config)


def test_schematic_parity_and_erc(tiny):
    _, res = tiny
    s = res["c1"]["summary"]
    assert s["parity"]["ok"] and s["parity"]["matched"] == 5
    assert s["erc"]["errors"] == 0
    assert s["netclasses"] == {"Power": 3}


def test_board_routed_and_clean(tiny):
    proj, res = tiny
    assert res["c2"]["summary"]["size_estimated"]
    assert res["c4"]["summary"]["unrouted"] == 0
    drc = res["c5"]["summary"]["drc"]
    assert drc["errors"] == 0 and drc["unconnected"] == 0 and drc["parity"] == 0
    decaps = res["c5"]["summary"]["decaps"]
    assert decaps["checked"] == 2 and not decaps["far"]  # the LDO's caps, at its pins
    pro = json.load(open(proj.kfile(".kicad_pro")))
    assert "Power" in [c["name"] for c in pro["net_settings"]["classes"]]


def test_fab_outputs(tiny):
    proj, res = tiny
    s = res["c6"]["summary"]
    fab = os.path.join(proj.out, "fab")
    assert os.path.exists(os.path.join(fab, "tiny-gerbers.zip"))
    assert s["gerbers"] >= 10
    bom = open(os.path.join(fab, "tiny-bom-jlcpcb.csv")).read()
    assert "C51118" in bom  # LCSC number from the part's field
    cpl = open(os.path.join(fab, "tiny-cpl-jlcpcb.csv")).read().splitlines()
    assert len(cpl) - 1 == s["placements"] == 6  # fiducials aren't in the BOM, so dropped
    assert s["analysis"]["release_gate"]["fail"] == 0


def test_rerun_one_stage(tiny):
    """A stage can be rerun alone: C4 starts from C3's snapshot, not its own output."""
    from boardsmith.physical import Pipeline

    proj, first = tiny
    pipe = Pipeline(proj, KICAD)
    (res,) = pipe.run(("c4",))
    assert res["summary"]["unrouted"] == 0
    assert res["summary"]["finish"]["pours_added"]  # pours added again: fresh board
