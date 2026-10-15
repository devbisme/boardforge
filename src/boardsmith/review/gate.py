"""B4 review gate (§4.10): checks constraints.json, the parts against their datasheet
extractions, the sourced BOM against the design, and the footprints against symbols and
packages; writes reports/review.json. Status per result: PASS, FAIL or NOT CHECKED;
the gate fails on a HARD failure and warns on a SOFT failure or anything not checked."""

import json
import os
import time

from boardsmith import datasheets
from boardsmith.review import constraints as cons
from boardsmith.review import bom_check, budget, datasheet_check, package_check, spice_path


def review(proj):
    cfg = proj.cfg.get("review", {})
    netlist_file = os.path.join(proj.out, "netlist.json")
    path = cons.path(proj)
    cs, problems = cons.load(path)
    results = []
    netlist = None
    if os.path.exists(netlist_file):
        with open(netlist_file) as f:
            netlist = json.load(f)
    else:
        problems.append("no netlist: run `boardsmith sch` (C1) first")
    facts = {}
    if netlist:
        store, _ = datasheets.stores(proj)
        for comp in netlist["components"]:
            mpn = datasheets.mpn_of(comp)
            if mpn and mpn not in facts:
                facts[mpn] = datasheets.amp_facts(store, mpn)
    ngspice = spice_path.find_ngspice(cfg.get("ngspice", ""))
    work = os.path.join(proj.out, "work", "spice")
    for c in cs:
        t0 = time.time()
        if c.get("invalid"):
            r = {"id": c["id"], "kind": c.get("kind"), "tag": c["tag"], "status": "NOT CHECKED",
                 "why": "invalid constraint (see problems)"}
        elif c.get("kind") == "ac_gain" and netlist:
            r = spice_path.check(netlist, c, cfg.get("models", {}), ngspice, cfg.get("dc", {}),
                                 os.path.join(work, c["id"]), {k: v for k, v in facts.items() if v})
        elif c.get("kind") == "rail_current" and netlist:
            r = budget.check(netlist, c, store)
        else:
            r = {"id": c["id"], "kind": c.get("kind"), "tag": c["tag"], "status": "NOT CHECKED",
                 "why": f"no checker for kind {c.get('kind')!r}"}
        r["seconds"] = round(time.time() - t0, 2)
        if c.get("why"):
            r["requirement"] = c["why"]
        results.append(r)
    if netlist:
        t0 = time.time()
        for r in datasheet_check.check(netlist, store, cfg.get("dc", {}),
                                       set(proj.cfg["datasheets"]["prefixes"])):
            r["seconds"] = round(time.time() - t0, 2)
            results.append(r)
        t0 = time.time()
        r = bom_check.check(netlist, os.path.join(proj.root, cfg.get("bom", "sourcing/sourced_bom.csv")))
        r["seconds"] = round(time.time() - t0, 2)
        results.append(r)
        t0 = time.time()
        for r in package_check.check(netlist, store, package_check.lib_paths(proj)):
            r["seconds"] = round(time.time() - t0, 2)
            results.append(r)
    hard_fail = [r["id"] for r in results if r["status"] == "FAIL" and r["tag"] == "HARD"]
    soft_fail = [r["id"] for r in results if r["status"] == "FAIL" and r["tag"] != "HARD"]
    unchecked = [r["id"] for r in results if r["status"] == "NOT CHECKED"]
    status = "FAIL" if hard_fail else "WARN" if (soft_fail or unchecked or problems or not cs) else "PASS"
    report = {"status": status, "constraints_file": os.path.relpath(path, proj.root),
              "counts": {s: sum(r["status"] == s for r in results)
                         for s in ("PASS", "FAIL", "NOT CHECKED")},
              "hard_fail": hard_fail, "soft_fail": soft_fail, "not_checked": unchecked,
              "problems": problems + ([] if cs else [f"no constraints in {os.path.relpath(path, proj.root)}"]),
              "results": results}
    os.makedirs(proj.reports, exist_ok=True)
    with open(os.path.join(proj.reports, "review.json"), "w") as f:
        json.dump(report, f, indent=1)
    return report
