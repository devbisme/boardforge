"""Run a SKiDL design and write its generic netlist and labels-only schematic (C1, part 1).

Runs in a subprocess (``python -m boardsmith.skidl_runner job.json``) so SKiDL's global
state, logging and working directory stay out of the orchestrator.

Job keys: design, top, name, title, symbol_dir, out_json, sch_dir, result.
Result keys: ok, components, nets, nc_nets, sheets, erc_errors, erc_warnings,
sym_libs {nickname: path}, fp_libs [nickname], warnings.
"""

import builtins
import json
import os
import runpy
import sys
import traceback


def _lib_path(lib, nick, search):
    """Absolute path of the file or symdir a SKiDL library was read from."""
    fp = getattr(lib, "filepath", "") or ""
    if fp and os.path.exists(fp):
        return os.path.abspath(fp)
    for d in search:
        for ext in (".kicad_symdir", ".kicad_sym"):
            cand = os.path.join(d, nick + ext)
            if os.path.exists(cand):
                return os.path.abspath(cand)
    return ""


def run(job):
    import skidl
    from skidl import KICAD10, lib_search_paths
    from skidl.logger import erc_logger
    from skidl.schematic_netlist import build_generic_netlist
    from schematizer import render

    warnings = []
    design = job["design"]
    design_dir = os.path.dirname(design)
    os.chdir(design_dir)
    sys.path.insert(0, design_dir)
    skidl.set_default_tool(KICAD10)
    # The design may add its own library directories; it should not need to name KiCad's.
    lib_search_paths[KICAD10] = [p for p in (job.get("symbol_dir"), design_dir) if p]
    skidl.config.pickle_dir = os.path.join(job["sch_dir"], "..", "work", "skidl_pickles")

    # run_name != "__main__", so the design's own command-line block doesn't run.
    mod = runpy.run_path(design, run_name="boardsmith_design")
    if job.get("top"):
        mod[job["top"]]()
    ckt = builtins.default_circuit
    if not ckt.parts:
        raise RuntimeError("the design created no parts (is [design] top set?)")

    ckt.ERC()
    erc = {"erc_errors": erc_logger.error.count, "erc_warnings": erc_logger.warning.count}

    ckt.merge_net_names()
    ckt.merge_nets()
    doc = build_generic_netlist(ckt, title=job.get("title", ""), top_name=job["name"])
    with open(job["out_json"], "w") as f:
        json.dump(doc, f, indent=1)

    os.makedirs(job["sch_dir"], exist_ok=True)
    render(doc, tool="kicad10", filepath=job["sch_dir"], top_name=job["name"],
           title=job.get("title", ""), mode="labels")

    sym_libs, fp_libs, no_mpn = {}, set(), []
    for p in ckt.parts:
        if p.ref.startswith("#"):
            continue
        lib = getattr(p, "lib", None)
        fn = getattr(lib, "filename", "") or ""
        if fn:
            nick = os.path.basename(fn).split(".kicad_sym")[0]
            path = _lib_path(lib, nick, lib_search_paths[KICAD10])
            if path:
                sym_libs[nick] = path
            else:
                warnings.append(f"symbol library {nick} not located for sym-lib-table")
        fp = getattr(p, "footprint", "") or ""
        if ":" in fp:
            fp_libs.add(fp.split(":")[0])
        else:
            warnings.append(f"{p.ref} has no footprint library ({fp!r})")
        if not (getattr(p, "fields", {}) or {}).get("MPN"):
            no_mpn.append(p.ref)
    if no_mpn:
        warnings.append(f"{len(no_mpn)} parts have no MPN field: {', '.join(sorted(no_mpn)[:12])}"
                        + (" ..." if len(no_mpn) > 12 else ""))

    return dict(
        ok=True,
        components=len(doc["components"]),
        nets=sum(1 for n in doc["nets"] if not n.get("nc")),
        nc_nets=sum(1 for n in doc["nets"] if n.get("nc")),
        sheets=len(doc["sheets"]),
        sym_libs=sym_libs,
        fp_libs=sorted(fp_libs),
        warnings=warnings,
        **erc,
    )


def main():
    with open(sys.argv[1]) as f:
        job = json.load(f)
    try:
        res = run(job)
    except Exception as e:  # noqa: BLE001 - reported to the orchestrator
        res = {"ok": False, "error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()}
    with open(job["result"], "w") as f:
        json.dump(res, f, indent=1)


if __name__ == "__main__":
    main()
