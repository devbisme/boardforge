"""Unit tests that need neither KiCad nor SKiDL."""

import json
import os
import re

import pytest

from boardsmith import analysis, config, drc, fabs, project, routers
from boardsmith.netlist import compare, read_kicad_netlist
from boardsmith.routers.specctra import prepare_dsn
from boardsmith.state import State

NETLIST = """(export (version "E")
  (components
    (comp (ref "R1") (value "10k") (footprint "Resistor_SMD:R_0603_1608Metric")
      (fields (field (name "MPN") "RC0603FR-0710KL") (field (name "Footprint") "x"))
      (sheetpath (names "/sub1/") (tstamps "/aaa/")) (tstamps "bbb"))
    (comp (ref "C1") (value "100nF") (footprint "Capacitor_SMD:C_0603_1608Metric")
      (sheetpath (names "/") (tstamps "/")) (tstamps "ccc")))
  (nets
    (net (code "1") (name "GND") (node (ref "R1") (pin "2")) (node (ref "C1") (pin "2")))
    (net (code "2") (name "/sub1/SIG") (node (ref "R1") (pin "1")) (node (ref "C1") (pin "1")))
    (net (code "3") (name "unconnected-(U1-NC-Pad3)") (node (ref "U1") (pin "3")))))
"""

DOC = {
    "nets": [
        {"name": "GND", "nodes": [{"ref": "R1", "pin": "2"}, {"ref": "C1", "pin": "2"},
                                  {"ref": "#PWR1", "pin": "1"}],
         "attrs": {"netclasses": ["Power"]}},
        {"name": "SIG", "nodes": [{"ref": "R1", "pin": "1"}, {"ref": "C1", "pin": "1"}],
         "attrs": {"netclasses": ["Fine", "Power"]}},
        {"name": "__NOCONNECT", "nc": True, "nodes": [{"ref": "U1", "pin": "3"}]},
    ],
    "netclasses": {"Power": {"track_width": 0.3, "priority": 2},
                   "Fine": {"trace_width": 0.1, "clearance": 0.1, "priority": 1, "color": "red"}},
}


@pytest.fixture
def kicad_net(tmp_path):
    p = tmp_path / "x.net"
    p.write_text(NETLIST)
    return str(p)


def test_read_kicad_netlist(kicad_net):
    comps, nets = read_kicad_netlist(kicad_net)
    assert comps["R1"]["fields"]["MPN"] == "RC0603FR-0710KL"
    assert comps["R1"]["sheettstamps"] + comps["R1"]["tstamp"] == "/aaa/bbb"
    assert nets["/sub1/SIG"] == [("R1", "1"), ("C1", "1")]


def test_compare_maps_names_and_ignores_power_refs(kicad_net):
    _, nets = read_kicad_netlist(kicad_net)
    res = compare(DOC, nets)
    assert res["ok"]
    assert res["net_map"] == {"GND": "GND", "SIG": "/sub1/SIG"}


def test_compare_reports_mismatch(kicad_net):
    _, nets = read_kicad_netlist(kicad_net)
    nets["/sub1/SIG"] = [("R1", "1")]
    res = compare(DOC, nets)
    assert not res["ok"]
    assert res["missing"] == ["SIG"] and res["extra"] == ["/sub1/SIG"]


def test_netclasses_priority_and_aliases():
    rules = config.DEFAULTS["rules"]
    classes, assign, warns = project.netclasses_from_doc(DOC, {"GND": "GND", "SIG": "/sub1/SIG"}, rules)
    by = {c["name"]: c for c in classes}
    # SKiDL: larger priority wins; KiCad: smaller wins.
    assert by["Power"]["priority"] < by["Fine"]["priority"]
    assert by["Fine"]["track_width"] == 0.1  # trace_width alias
    assert by["Power"]["clearance"] == rules["clearance"]  # unset -> default class
    assert assign == {"GND": ["Power"], "/sub1/SIG": ["Fine", "Power"]}
    assert any("color" in w for w in warns)


def test_write_netclasses_is_idempotent(tmp_path):
    pro = str(tmp_path / "p.kicad_pro")
    rules = config.DEFAULTS["rules"]
    classes, assign, _ = project.netclasses_from_doc(DOC, {"GND": "GND", "SIG": "/sub1/SIG"}, rules)
    project.write_netclasses(pro, classes, assign, rules)
    first = open(pro).read()
    d = json.loads(first)
    d["board"] = {"keep": 1}  # what pcbnew writes must survive
    json.dump(d, open(pro, "w"))
    project.write_netclasses(pro, classes, assign, rules)
    d = json.load(open(pro))
    assert d["board"] == {"keep": 1}
    assert [c["name"] for c in d["net_settings"]["classes"]] == ["Default", "Power", "Fine"]
    assert {"netclass": "Fine", "pattern": "/sub1/SIG"} in d["net_settings"]["netclass_patterns"]


def test_lib_tables(tmp_path):
    (tmp_path / "power.kicad_symdir").mkdir()
    fp = project.footprint_paths(["Resistor_SMD", "Mine"], "/fp", {"Mine": "/my/Mine.pretty"})
    assert fp == {"Mine": "/my/Mine.pretty", "Resistor_SMD": "/fp/Resistor_SMD.pretty"}
    missing = project.write_lib_tables(str(tmp_path), {"Device": "/s/Device.kicad_symdir"}, fp,
                                       str(tmp_path))
    assert set(missing) == {"Mine", "Resistor_SMD"}
    sym = (tmp_path / "sym-lib-table").read_text()
    assert '(name "Device")' in sym and '(name "power")' in sym


def test_config_merge(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "USER_CONFIG", str(tmp_path / "none.toml"))
    (tmp_path / "boardsmith.toml").write_text(
        '[design]\nfile = "d.py"\n[board]\nlayers = 2\n[routing.astar]\npasses = 3\n')
    p = config.Project.load(str(tmp_path))
    assert p.name == "d"
    assert p.cfg["board"]["layers"] == 2 and p.cfg["board"]["fill"] == 0.35
    assert p.cfg["routing"]["astar"] == {"passes": 3, "max_expansions": 600000, "repairs": 3}
    assert p.kfile(".kicad_pcb").endswith(os.path.join("build", "kicad", "d.kicad_pcb"))


def test_config_requires_design(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "USER_CONFIG", str(tmp_path / "none.toml"))
    with pytest.raises(ValueError):
        config.Project.load(str(tmp_path))


def test_prepare_dsn():
    dsn = ('(structure (layer F.Cu (type signal)) (layer In1.Cu (type signal))\n'
           '  (layer B.Cu (type signal)))\n'
           '(network (net GND (pins U1-1 U1-2 C1-2)) (net "+3V3" (pins U1-3 C1-1))'
           ' (net SIG (pins U1-4 R1-1)))')
    out, notes = prepare_dsn(dsn, ["GND", "+3V3"], ["In1.Cu"])
    assert "(net GND (pins U1-1))" in out
    assert '(net "+3V3" (pins U1-3))' in out
    assert "(net SIG (pins U1-4 R1-1))" in out
    assert "(layer In1.Cu (type power))" in out and "(layer F.Cu (type signal))" in out


def test_router_registry():
    assert set(routers.ROUTERS) == {"freerouting", "astar"}
    with pytest.raises(ValueError):
        routers.get("nope")
    assert routers.get("freerouting").capabilities()["planes"] is False


def test_freerouting_command_disables_neckdown(tmp_path):
    exe = tmp_path / "freerouting"
    exe.write_text("")
    job = routers.RouteJob(board="b", workdir=str(tmp_path), kicad=None,
                           options={"path": str(exe), "max_passes": 5})
    cmd = routers.get("freerouting").command("in.dsn", "out.ses", job)
    assert cmd[:7] == [str(exe), "-de", "in.dsn", "-do", "out.ses", "-mp", "5"]
    assert "--router.automatic_neckdown=false" in cmd
    assert "--router.neck_width_um=150" in cmd  # retries at the default width (0.15 mm)
    job.options["neckdown"] = True
    assert not [c for c in routers.get("freerouting").command("in.dsn", "out.ses", job) if "neck" in c]


def test_repairable_drc_errors():
    def v(typ, *items, sev="error"):
        return {"type": typ, "severity": sev, "items": [{"description": d} for d in items]}

    report = {"violations": [
        v("clearance", "Track [A] on F.Cu", "Track [B] on F.Cu"),
        v("track_width", "Track [A] on F.Cu"),
        v("clearance", "Pad 1 [A] of U1", "Pad 2 [B] of U1"),  # footprint problem: not ours
        v("silk_overlap", "Track [A] on F.Cu"),
        v("clearance", "Via [A]", "Pad 1 [B] of U1", sev="warning"),
    ]}
    assert [x["type"] for x in drc.repairable(report)] == ["clearance", "track_width"]


def test_jlcpcb_profile():
    fab = fabs.get("jlcpcb")
    assert "jlcpcb" in fabs.available()
    rules, notes = fab.design_rules(4)
    assert rules["min_track"] == 0.09 and not notes
    _, notes = fab.design_rules(2)
    assert notes  # 2 layers is outside the multilayer tier
    assert fab.check_rules({"track_width": 0.05, "via_diameter": 0.45, "via_drill": 0.2})
    assert fab.rotation_offset("Package_TO_SOT_SMD:SOT-23-6") == 180
    assert fab.rotation_offset("Package_TO_SOT_SMD:SOT-223-3_TabPin2") == 180
    assert fab.rotation_offset("Resistor_SMD:R_0603_1608Metric") == 0
    assert "In2.Cu" in fab.gerber_layers(4) and "In1.Cu" not in fab.gerber_layers(2)
    assert fab.dfm_checks({"board_mm": [600, 10]})
    with pytest.raises(ValueError):
        fabs.get("nofab")


def test_jlcpcb_bom_cpl(tmp_path):
    fab = fabs.get("jlcpcb")
    kbom = tmp_path / "bom.csv"
    kbom.write_text('"Reference","Value","Footprint","MPN","Manufacturer","LCSC","Quantity","DNP"\n'
                    '"C1,C2","100nF","Capacitor_SMD:C_0603_1608Metric","GRM188","Murata","","2",""\n'
                    '"U1","USBLC6","Package_TO_SOT_SMD:SOT-23-6","USBLC6-2SC6","ST","","1",""\n')
    out = tmp_path / "jbom.csv"
    res = fab.bom(str(kbom), str(out), {"C1": {"LCSC": "C14663"}, "C2": {"LCSC": "C14663"}})
    rows = list(__import__("csv").reader(open(out)))
    assert rows[1][3] == "C14663" and res["lines_without_lcsc"] == 1
    pos = tmp_path / "pos.csv"
    pos.write_text("Ref,Val,Package,PosX,PosY,Rot,Side\n"
                   "C1,100nF,C_0603,10,20,90,top\nU1,USBLC6,SOT-23-6,30,40,270,bottom\n"
                   "FID1,Fiducial,F,1,1,0,top\n")
    cpl = tmp_path / "cpl.csv"
    res = fab.cpl(str(pos), str(cpl), {"U1": "Package_TO_SOT_SMD:SOT-23-6"}, bom_csv=str(out))
    rows = list(__import__("csv").reader(open(cpl)))
    assert [r[0] for r in rows[1:]] == ["C1", "U1"]  # FID1 isn't in the BOM
    assert rows[2][4] == "90" and res["rotation_adjusted"] == ["U1"]  # 270 + 180


def test_analysis_registry_override(tmp_path):
    calls = []

    @analysis.register("thermal", "test_impl")
    def _t(ctx):
        calls.append(ctx.outputs.get("pcb"))
        p = ctx.out("thermal")
        json.dump({"summary": {"by_severity": {"error": 1}},
                   "findings": [{"severity": "error", "rule_id": "TS-1", "summary": "hot"}]}, open(p, "w"))
        return p

    ctx = analysis.Context("s", "p", str(tmp_path))
    ctx.outputs["pcb"] = "pcb.json"
    res = analysis.run_steps(ctx, ["thermal", "cross_verify"], {"thermal": "test_impl"})
    assert calls == ["pcb.json"]
    assert res["thermal"]["errors"] == ["TS-1: hot"]
    assert "skipped" in res["cross_verify"]  # needs the schematic analysis
    res = analysis.run_steps(ctx, ["thermal"], {"thermal": "missing"})
    assert "error" in res["thermal"]


def test_rail_currents_replace_load_guesses():
    sch = {"findings": [
        {"detector": "detect_power_regulators", "ref": "U2", "output_rail": "+1V2",
         "power_dissipation": {"dropout_V": 2.1, "estimated_iout_A": 0.204, "estimated_pdiss_W": 0.428}},
        {"detector": "detect_power_regulators", "ref": "U1", "output_rail": "+3V3",
         "power_dissipation": {"vout_V": 3.3, "efficiency_assumed": 0.85, "estimated_iout_A": 0.6}},
        {"detector": "detect_power_regulators", "ref": "U9", "output_rail": "+5V"},
    ], "power_budget": {"rails": {"+3V3": {"estimated_load_mA": 600}}}}
    got = analysis.apply_rail_currents(sch, {"+1V2": 0.05, "+3V3": 0.25})
    assert got == [("U2", "+1V2", 0.05), ("U1", "+3V3", 0.25)]
    pd = sch["findings"][0]["power_dissipation"]
    assert pd["estimated_iout_A"] == 0.05 and pd["estimated_pdiss_W"] == 0.105
    assert sch["findings"][1]["power_dissipation"]["estimated_pdiss_W"] == round(3.3 * 0.25 * (1 / 0.85 - 1), 4)
    assert sch["power_budget"]["rails"]["+3V3"]["estimated_load_mA"] == 250
    assert "estimated_iout_A" not in sch["findings"][2]


def test_release_gate_ignores_board_only_footprints(tmp_path, monkeypatch):
    seen = {}

    def fake_vendor(script, *args, **kw):
        pcb = json.load(open(args[args.index("-p") + 1]))
        seen["count"] = pcb["statistics"]["footprint_count"]

    monkeypatch.setattr(analysis, "_vendor", fake_vendor)
    ctx = analysis.Context("s", "p", str(tmp_path))
    pcb = tmp_path / "pcb.json"
    pcb.write_text(json.dumps({"statistics": {"footprint_count": 4}, "footprints": [
        {"reference": "R1"}, {"reference": "U1"}, {"reference": "FID1", "board_only": True},
        {"reference": "H1", "board_only": True}]}))
    ctx.outputs.update(schematic="sch.json", pcb=str(pcb))
    analysis.REGISTRY["release_gate"]["kicad_happy"](ctx)
    assert seen["count"] == 2


def test_state_marks_later_stages_stale(tmp_path):
    st = State(str(tmp_path / "s.json"))
    for sid in ("c1", "c2", "c3"):
        st.record(sid, "done", {})
    st.record("c2", "warn", {}, ["x"])
    st = State(str(tmp_path / "s.json"))
    assert st.stage("c3")["status"] == "stale"
    assert st.next_stage() == "c3"


def test_stable_uuids_make_board_files_repeatable(tmp_path):
    from boardsmith.pcbfile import stable_uuids

    def fp(ref, u):
        return (f'\t(footprint "R"\n\t\t(layer "F.Cu")\n\t\t(uuid "{u}")\n'
                f'\t\t(property "Reference" "{ref}"\n\t\t\t(uuid "{ref}-field")\n\t\t)\n\t)\n')

    def seg(x, u):
        return f'\t(segment\n\t\t(start {x} 0)\n\t\t(end 1 1)\n\t\t(uuid "{u}")\n\t)\n'

    head = '(kicad_pcb\n\t(version 20250114)\n\t(net 0 "")\n'
    a = head + fp("R1", "a1") + fp("R2", "a2") + seg(0, "s1") + seg(0, "s2") + seg(5, "s3") + ")\n"
    b = head + fp("R2", "b9") + fp("R1", "b8") + seg(5, "t1") + seg(0, "t2") + seg(0, "t3") + ")\n"
    pa, pb = tmp_path / "a.kicad_pcb", tmp_path / "b.kicad_pcb"
    pa.write_text(a)
    pb.write_text(b)
    stable_uuids(pa)
    stable_uuids(pb)
    out = pa.read_text()
    assert out == pb.read_text()
    assert out.endswith(")\n") and out.count("(segment") == 3
    uuids = re.findall(r'(?m)^\t\t\(uuid "([^"]+)"\)', out)
    assert len(set(uuids)) == 5  # identical segments still get distinct UUIDs
    assert '"R1-field"' in out  # only an item's own UUID changes


def test_gate_routing_status_comes_from_drc():
    pcb = {"connectivity": {"unrouted_count": 0, "routing_complete": True}}
    report = {"unconnected_items": [{"items": [
        {"description": "Pad 55 [DA10] of U12 on F.Cu"},
        {"description": "Track [DA10] on F.Cu, length 0.16 mm"}]}]}
    assert analysis.routing_from_drc(pcb, report) == ["DA10"]
    assert pcb["connectivity"]["unrouted_count"] == 1 and not pcb["connectivity"]["routing_complete"]
    assert analysis.routing_from_drc(pcb, {"unconnected_items": []}) == []
    assert pcb["connectivity"]["routing_complete"]


def test_pin_swap_orders_bus_around_both_outlines(monkeypatch):
    import importlib
    import sys
    import types

    stub = types.ModuleType("pcbnew")
    stub.FromMM = stub.ToMM = lambda v: v
    monkeypatch.setitem(sys.modules, "pcbnew", stub)
    for m in ("boardsmith.pcb.common", "boardsmith.pcb.pinswap"):
        sys.modules.pop(m, None)
    ps = importlib.import_module("boardsmith.pcb.pinswap")
    # A QFN at the origin with pads on its top, right and bottom edges; an FPGA to the right
    # with a column of pins on its left edge (y grows downwards, as in KiCad).
    pads = [((-1, -2), "t0"), ((1, -2), "t1"), ((2, -1), "r0"), ((2, 1), "r1"),
            ((1, 2), "b1"), ((-1, 2), "b0")]
    sig = ps._around(pads, (0, 0))
    assert sig == ["t0", "t1", "r0", "r1", "b1", "b0"]  # clockwise from the free left side
    pins = [((10, -3 + i), str(i + 1)) for i in range(8)]
    pin = ps._around(pins, (14, 0))[::-1]
    assert pin == [str(i + 1) for i in range(8)]  # top to bottom down the left edge
    pos = dict((k, p) for p, k in pads)
    pin_xy = dict((k, p) for p, k in pins)
    chosen = ps._ordered_assign([pos[s] for s in sig], [pin_xy[n] for n in pin])
    assert chosen == sorted(chosen) and len(set(chosen)) == len(sig)
    # The leftmost top pad goes outermost (top pin): its trace leaves upwards and wraps
    # around the others.
    assert chosen[0] == 0


def test_pin_swap_far_row_of_a_tsop_faces_away(monkeypatch):
    import importlib
    import sys
    import types

    stub = types.ModuleType("pcbnew")
    stub.FromMM = stub.ToMM = lambda v: v
    monkeypatch.setitem(sys.modules, "pcbnew", stub)
    for m in ("boardsmith.pcb.common", "boardsmith.pcb.pinswap"):
        sys.modules.pop(m, None)
    ps = importlib.import_module("boardsmith.pcb.pinswap")

    class P:
        def __init__(self, x, y):
            self.x, self.y = x, y

        def GetPosition(self):
            return self

    class Part(P):
        def Pads(self):
            # TSOP: two long rows at x = +-5, pins from y = -10 to 10
            return [P(self.x + sx, self.y + y) for sx in (-5, 5) for y in range(-10, 11)]

    tsop = Part(30, 0)
    fpga = (0, 0)  # to the left
    assert not ps._faces_away((25, -10), tsop, fpga)  # near row, even at its end
    assert ps._faces_away((35, -10), tsop, fpga)  # far row, at its end
    assert ps._faces_away((35, 0), tsop, fpga)


def test_compaction_steps_back_up_when_routing_fails(tmp_path, monkeypatch):
    from boardsmith.physical import Pipeline

    (tmp_path / "d.py").write_text("")
    (tmp_path / "boardsmith.toml").write_text(
        '[design]\nfile = "d.py"\n[placement.compact]\nenabled = true\nroute_retries = 3\n')
    proj = config.Project.load(str(tmp_path))
    pipe = Pipeline.__new__(Pipeline)
    pipe.p, pipe.cfg = proj, proj.cfg
    pipe.state = State(proj.state_file)
    pipe.scale, pipe.base_size = 1.0, None
    calls = []

    def stage(sid, summary=None):
        def fn():
            calls.append((sid, pipe.scale))
            s = dict(summary or {})
            if sid == "c2":
                s["size"] = [100, 80]
            if sid == "c4":  # routes only at 0.9 or larger
                s["unrouted"] = 0 if pipe.scale >= 0.9 - 1e-9 else 2
            return "done", s, []
        return fn

    for sid in ("c1", "c2", "c3", "c4", "c5", "c6"):
        monkeypatch.setattr(pipe, sid, stage(sid))

    def compact():
        pipe.scale = 0.8
        return [{}]

    monkeypatch.setattr(pipe, "compact", compact)
    out = pipe._run(["c1", "c2", "c3", "c4", "c5", "c6"], None)
    c4 = [c for c in calls if c[0] == "c4"]
    assert c4 == [("c4", 0.8), ("c4", 0.85), ("c4", 0.9)]  # stepped back up until it routed
    assert out[-1]["stage"] == "c6" and pipe.scale == 0.9
