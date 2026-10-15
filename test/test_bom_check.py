"""BOM check (ported validate-bom): the sourced BOM CSV against the built design."""

from boardsmith.review import bom_check as bc


def _nl():
    def c(ref, value, fp, lib, mpn=None):
        return {"ref": ref, "value": value, "footprint": fp, "lib_id": lib,
                "fields": {"MPN": mpn} if mpn else {}}
    return {"components": [
        c("R1", "10k", "Resistor_SMD:R_0603_1608Metric", "Device:R", "RC0603FR-0710KL"),
        c("R2", "10k", "Resistor_SMD:R_0603_1608Metric", "Device:R", "RC0603FR-0710KL"),
        c("C1", "100nF", "Capacitor_SMD:C_0603_1608Metric", "Device:C"),
        c("U1", "W25Q32", "Package_SO:SOIC-8_5.3x5.3mm_P1.27mm", "Memory_Flash:W25Q32JVSS", "W25Q32JVSSIQ"),
        c("#FLG1", "PWR_FLAG", "", "power:PWR_FLAG")]}


HEAD = "refdes,mpn,value,package,footprint,symbol\n"
GOOD = HEAD + ('"R1,R2",RC0603FR-0710KL,10k,0603,Resistor_SMD:R_0603_1608Metric,Device:R\n'
               "C1,,0.1uF,0603,Capacitor_SMD:C_0603_1608Metric,Device:C\n"
               "U1,W25Q32JVSSIQ,W25Q32,SOIC-8-208mil,Package_SO:SOIC-8_5.3x5.3mm_P1.27mm,Memory_Flash:W25Q32JVSS\n")


def _check(tmp_path, text):
    p = tmp_path / "bom.csv"
    p.write_text(text)
    return bc.check(_nl(), str(p))


def _details(r):
    return [f["detail"] for f in r["findings"] if f["severity"] == "error"]


def test_bom_agrees(tmp_path):
    r = _check(tmp_path, GOOD)  # 100nF == 0.1uF; PWR_FLAGs aren't BOM parts
    assert r["status"] == "PASS" and _details(r) == [] and r["why"] == "4 parts, 3 BOM rows"


def test_bom_mismatches(tmp_path):
    bad = (GOOD.replace("0.1uF", "220nF").replace("SOIC-8-208mil", "SOIC-8-150mil")
           .replace("W25Q32JVSSIQ,W25Q32", "W25Q64JVSSIQ,W25Q32").replace('"R1,R2"', "R1"))
    bad += "R9,,1k,,,\nR1,,10k,,,\n"
    r = _check(tmp_path, bad)
    d = " | ".join(_details(r))
    assert r["status"] == "FAIL" and r["tag"] == "HARD"
    for frag in ("C1: value", "U1: package 'SOIC-8-150mil'", "U1: MPN", "R9: in the BOM",
                 "R1: listed twice", "R2: in the design but not in the BOM"):
        assert frag in d, frag
    assert set(r["refs"]) == {"C1", "U1", "R9", "R1", "R2"}


def test_bom_lcsc(tmp_path):
    good = GOOD.replace("refdes,mpn,", "refdes,lcsc,mpn,").replace('"R1,R2",', '"R1,R2",C25804,')
    good = good.replace("\nC1,,", "\nC1,,,").replace("\nU1,", "\nU1,C82344,")
    nl = _nl()
    nl["components"][0]["fields"]["LCSC"] = "C25804"
    nl["components"][1]["fields"]["LCSC"] = "C99999"
    p = tmp_path / "bom.csv"
    p.write_text(good)
    r = bc.check(nl, str(p))
    d = [(f["severity"], f["detail"][:40]) for f in r["findings"] if f["severity"] != "info"]
    assert ("error", "R2: LCSC: BOM 'C25804' vs design 'C99999") in d
    assert ("warning", "U1: no LCSC field in the design; BOM say") in d


def test_bom_missing_or_unreadable(tmp_path):
    r = bc.check(_nl(), str(tmp_path / "nope.csv"))
    assert r["status"] == "NOT CHECKED" and "no BOM CSV" in r["why"]
    assert _check(tmp_path, "a,b\n1,2\n")["status"] == "FAIL"
    assert bc.values_agree("4k7", "4.7k") and not bc.values_agree("200pF", "220pF")
    assert bc.expand_refdes("R1,R2 C10-C12") == ["R1", "R2", "C10", "C11", "C12"]
