"""Fab-output corrections: LCSC numbers on ranged BOM lines, DFM tier against the fab tier."""

from boardsmith import fabs
from boardsmith.analysis import dfm_for_fab
from boardsmith.fabs.base import read_csv

KBOM = '''"Reference","Value","Footprint","MPN","Manufacturer","LCSC","Quantity","DNP"
"C1,C7-C9","1uF","Capacitor_SMD:C_0402_1005Metric","CL05A105KA5NQNC","","C52923","4",""
"FB1-FB4","600R","Inductor_SMD:L_0603_1608Metric","BLM18AG601SN1D","","C19330","4",""
'''


def test_jlcpcb_bom_fills_lcsc_on_ranged_lines(tmp_path):
    src, out = tmp_path / "bom.csv", tmp_path / "jlc.csv"
    src.write_text(KBOM)
    fields = {r: {"LCSC": "C52923"} for r in ("C1", "C7", "C8", "C9")}
    fields.update({f"FB{i}": {"LCSC": "C19330"} for i in range(1, 5)})
    res = fabs.get("jlcpcb").bom(str(src), str(out), fields)
    rows = read_csv(str(out))
    lcsc = {r[rows[0].index("Designator")]: r[rows[0].index("LCSC Part #")] for r in rows[1:]}
    assert res["lines_without_lcsc"] == 0 and "C19330" in lcsc.values()


def _finding(par, actual, sev="warning"):
    return {"rule_id": "DFM-001", "category": "dfm", "parameter": par, "actual_mm": actual,
            "severity": sev}


def test_dfm_tier_judged_against_fab_tier():
    limits = {"name": "jlcpcb/multilayer", "min_track": 0.09, "min_clearance": 0.09,
              "min_drill": 0.15, "annular_ring": 0.05}
    pcb = {"dfm_summary": {"dfm_tier": "advanced"},
           "findings": [_finding("track_spacing", 0.1017), _finding("board_size", [120, 90], "info"),
                        {"rule_id": "TB-001", "category": "dfm"}]}
    assert dfm_for_fab(pcb, limits) == ["track_spacing"]
    assert pcb["dfm_summary"]["dfm_tier"] == "standard" and len(pcb["findings"]) == 2
    # below the fab tier's own minimum: kept, tier unchanged
    pcb = {"dfm_summary": {"dfm_tier": "advanced"}, "findings": [_finding("track_spacing", 0.08)]}
    assert dfm_for_fab(pcb, limits) == [] and pcb["dfm_summary"]["dfm_tier"] == "advanced"
    assert dfm_for_fab(pcb, None) == []
