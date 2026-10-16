"""Part-fact cache (§4.10 gate 3): datasheets status/sync/publish, PDF identity, facts."""

import json
import os
import shutil

import pytest

from boardforge import datasheets as ds
from boardforge.config import Project

EXAMPLE = os.path.join(os.path.dirname(ds.SCHEMAS), "examples", "lm358.json")


def _pdf(path, text):
    """A minimal one-page PDF showing ``text`` (enough for pdftotext)."""
    lines = [text[i:i + 60] for i in range(0, len(text), 60)]
    stream = ("BT /F1 10 Tf 14 TL 72 720 Td " + " ".join(f"({s}) Tj T*" for s in lines) + " ET").encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offs = bytearray(b"%PDF-1.4\n"), []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offs)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    with open(path, "wb") as f:
        f.write(out)
    return path


def _project(tmp_path, comps, **dscfg):
    (tmp_path / "d.py").write_text("")
    lines = ['[design]\nfile = "d.py"\n[datasheets]']
    lines += [f"{k} = {json.dumps(v)}" for k, v in {"cache": str(tmp_path / "shared"), **dscfg}.items()]
    (tmp_path / "boardforge.toml").write_text("\n".join(lines) + "\n")
    (tmp_path / "build").mkdir()
    nl = {"symbols": {}, "nets": [], "components": [
        {"ref": ref, "lib_id": "x:y", "value": ref, "fields": {"MPN": mpn} if mpn else {}, "pins": []}
        for ref, mpn in comps]}
    (tmp_path / "build" / "netlist.json").write_text(json.dumps(nl))
    return Project.load(str(tmp_path))


def _extraction(store, mpn, pdf_text, when="2026-01-01T00:00:00Z", score=80):
    """kicad-happy's LM358 example re-pointed at a fresh PDF for ``mpn``."""
    with open(EXAMPLE) as f:
        ex = json.load(f)
    os.makedirs(store.extracted, exist_ok=True)
    pdf = _pdf(os.path.join(store.root, ds.sanitize(mpn) + ".pdf"), pdf_text)
    ex["source"].update(mpn=mpn, local_path=os.path.basename(pdf), sha256=ds.sha256(pdf))
    ex["extraction"].update(extracted_at=when, quality_score=score)
    with open(store.extraction_file(mpn), "w") as f:
        json.dump(ex, f)
    return ex


def test_parts_and_names():
    nl = {"symbols": {"a:U": {"datasheet": "https://x/y.pdf"}}, "components": [
        {"ref": "U1", "lib_id": "a:U", "fields": {"mpn": "LP5907MFX-3.3/NOPB"}},
        {"ref": "U2", "lib_id": "a:U", "fields": {"MPN": "LP5907MFX-3.3/NOPB"}},
        {"ref": "R1", "lib_id": "a:R", "fields": {"MPN": "RC0603FR-071KL"}},
        {"ref": "C1", "lib_id": "a:C", "fields": {}}]}
    p = ds.parts(nl, {"U"})
    assert p["LP5907MFX-3.3/NOPB"]["refs"] == ["U1", "U2"] and p["LP5907MFX-3.3/NOPB"]["wanted"]
    assert p["LP5907MFX-3.3/NOPB"]["url"] == "https://x/y.pdf"
    assert not p["RC0603FR-071KL"]["wanted"] and len(p) == 2
    assert ds.sanitize("LP5907MFX-3.3/NOPB") == "LP5907MFX-3_3_NOPB"


@pytest.mark.skipif(not shutil.which("pdftotext"), reason="pdftotext not found")
def test_identify(tmp_path):
    pdf = _pdf(str(tmp_path / "a.pdf"), "AP2112 600mA CMOS LDO regulator " * 10)
    assert ds.identify(pdf, "AP2112K-1.2TRG1") is True  # the core name is enough
    assert ds.identify(pdf, "LP5907MFX-3.3/NOPB") is False  # a different part
    assert ds.identify(_pdf(str(tmp_path / "b.pdf"), "x"), "AP2112K") is None  # no text to check


def test_sync_from_cache_and_search(tmp_path):
    proj = _project(tmp_path, [("U1", "OPA810IDR"), ("U2", "TLV62569DBVR"), ("U3", "NOPE1"),
                               ("R1", "RC0603")],
                    search=[str(tmp_path / "pdfs")])
    project, shared = ds.stores(proj)
    _extraction(shared, "OPA810IDR", "OPA810 op amp " * 20)
    shared.add_pdf(os.path.join(shared.root, "OPA810IDR.pdf"), "OPA810IDR")
    shared.save()
    os.makedirs(tmp_path / "pdfs")
    _pdf(str(tmp_path / "pdfs" / "TLV62569.pdf"), "TLV62569 buck converter " * 20)

    res = ds.sync(proj, fresh=True, download=False, log=lambda m: None)
    assert res["from_cache"] == [] and res["pdf_local"] == ["TLV62569DBVR"]
    res = ds.sync(proj, download=False, log=lambda m: None)
    assert res["from_cache"] == ["OPA810IDR"] and res["pdf_missing"] == ["NOPE1"]
    assert res["to_extract"] == ["TLV62569DBVR"]  # has a PDF, no extraction; RC0603 not wanted
    project = ds.Store(project.root)
    assert project.extraction("OPA810IDR") and project.stale("OPA810IDR") is None
    rows = {r["mpn"]: r for r in ds.status(proj)}
    assert rows["OPA810IDR"]["extracted"] and rows["OPA810IDR"]["in_cache"]
    assert rows["TLV62569DBVR"]["pdf"] and not rows["TLV62569DBVR"]["extracted"]


def test_publish_rules(tmp_path):
    pytest.importorskip("jsonschema")
    proj = _project(tmp_path, [("U1", "LM358"), ("U2", "BAD1")])
    project, shared = ds.stores(proj)
    _extraction(project, "LM358", "LM358 " * 40, when="2026-02-01T00:00:00Z")
    bad = _extraction(project, "BAD1", "BAD1 " * 40)
    del bad["base"]["pinout"]
    with open(project.extraction_file("BAD1"), "w") as f:
        json.dump(bad, f)
    res = ds.publish(proj, log=lambda m: None)
    assert res == {"published": ["LM358"], "kept": [], "rejected": ["BAD1"]}
    assert ds.Store(shared.root).extraction("LM358") and ds.Store(shared.root).stale("LM358") is None
    assert ds.publish(proj, log=lambda m: None)["kept"] == ["LM358"]  # same date: kept
    _pdf(os.path.join(project.root, "LM358.pdf"), "changed")  # PDF no longer matches
    assert "LM358" in ds.publish(proj, log=lambda m: None)["rejected"]


def test_amp_facts_and_expose(tmp_path):
    proj = _project(tmp_path, [("U1", "LM358")])
    project, _ = ds.stores(proj)
    _extraction(project, "LM358", "LM358 " * 40)
    f = ds.amp_facts(project, "LM358")
    assert f["gbw_hz"] == 1e6 and f["source"] == "LM358 datasheet p7" and f["slew_vus"] > 0
    assert ds.amp_facts(project, "NOPE") is None
    assert ds.expose(proj) is None
    assert os.path.realpath(tmp_path / "build" / "datasheets") == os.path.realpath(project.root)


def test_add_check_show(tmp_path):
    pytest.importorskip("jsonschema")
    proj = _project(tmp_path, [("U1", "LM358")])
    project, _ = ds.stores(proj)
    other = _pdf(str(tmp_path / "other.pdf"), "TLV62569 buck converter " * 20)
    with pytest.raises(ValueError, match="doesn't name"):
        ds.add(proj, "LM358", other)
    with pytest.raises(ValueError, match="not a PDF"):
        ds.add(proj, "LM358", str(tmp_path / "d.py"))
    scanned = _pdf(str(tmp_path / "scan.pdf"), "x")  # no text to check: refused unless forced
    with pytest.raises(ValueError, match="no text to confirm"):
        ds.add(proj, "LM358", scanned)
    good = _pdf(str(tmp_path / "lm.pdf"), "LM358 dual operational amplifier " * 20)
    assert os.path.basename(ds.add(proj, "LM358", good)) == "LM358.pdf"
    assert project.pdf("LM358")
    # an agent-written extraction: no derived fields yet
    with open(EXAMPLE) as f:
        ex = json.load(f)
    del ex["extraction"], ex["schema_version"]
    ex["source"].update(mpn="LM358", sha256=None, local_path=None)
    os.makedirs(project.extracted, exist_ok=True)
    with open(project.extraction_file("LM358"), "w") as f:
        json.dump(ex, f)
    (row,) = ds.check(proj, ["LM358"])
    assert row["problems"] == [] and row["quality"] > 60
    done = project.extraction("LM358")
    assert done["source"]["sha256"] == ds.sha256(project.pdf("LM358")) and project.stale("LM358") is None
    assert ds.check(proj, ["NOPE"])[0]["problems"]
    text = ds.summary(done)
    assert text.startswith("# LM358") and "## Pinout" in text and "| Pin | Name |" in text


def test_parts_from_bom_before_code(tmp_path):
    proj = _project(tmp_path, [])
    os.remove(tmp_path / "build" / "netlist.json")
    os.makedirs(tmp_path / "sourcing")
    (tmp_path / "sourcing" / "sourced_bom.csv").write_text(
        'refdes,mpn,value\n"U1,U2",OPA810IDR,OPA810\nR1-R3,RC0603,10k\nU3,,X\n')
    parts = ds.project_parts(proj)
    assert parts["OPA810IDR"]["refs"] == ["U1", "U2"] and parts["OPA810IDR"]["wanted"]
    assert not parts["RC0603"]["wanted"] and len(parts["RC0603"]["refs"]) == 3
    os.remove(tmp_path / "sourcing" / "sourced_bom.csv")
    with pytest.raises(FileNotFoundError, match="sourced BOM"):
        ds.project_parts(proj)
