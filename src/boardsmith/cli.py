"""boardsmith command line.

    boardsmith run [-C DIR] [--from c3] [--to c6]   physical pipeline C1-C6 (or a range)
    boardsmith sch|board|place|route|check|fab       one stage (C1 ... C6)
    boardsmith review                                B4 gate: constraints.json against the design
    boardsmith datasheets status|sync|publish        part-fact cache (datasheet PDFs, extractions)
    boardsmith status                                stage results from pipeline_state.json
    boardsmith doctor                                check KiCad, Freerouting, config

Output is a short summary per stage; details are in <project>/build/reports/.
"""

import argparse
import json
import sys

from boardsmith import __version__, fabs, routers
from boardsmith.config import Project
from boardsmith.state import STAGES, TITLES, State

ALIASES = {"sch": "c1", "board": "c2", "place": "c3", "route": "c4", "check": "c5", "fab": "c6"}


def _print_stage(res, verbose):
    s = res["summary"]
    print(f"[{res['stage']}] {TITLES[res['stage']]}: {res['status']} ({res['seconds']} s)")
    text = json.dumps(s, indent=1 if verbose else None, default=str)
    print("  " + (text if verbose else text[:600] + ("..." if len(text) > 600 else "")))
    for w in res["warnings"][:12]:
        print(f"  ! {w}")
    if len(res["warnings"]) > 12:
        print(f"  ! ... {len(res['warnings']) - 12} more (pipeline_state.json)")


def cmd_run(args, stages):
    from boardsmith.physical import Pipeline

    from boardsmith.physical import StageError

    pipe = Pipeline(Project.load(args.dir))
    for w in pipe.k.warnings:
        print(f"! {w}")
    failed = False
    try:
        results = pipe.run(stages)
    except StageError as e:
        print(f"! {e}")
        return 1
    for res in results:
        _print_stage(res, args.verbose)
        failed |= res["status"] == "failed"
    return 1 if failed else 0


def cmd_review(args):
    from boardsmith.review.gate import review

    rep = review(Project.load(args.dir))
    print(f"[review] {rep['status']}: {rep['counts']} (reports/review.json)")
    for r in rep["results"]:
        detail = (f"{r['gain_db']} dB at {r['at_hz']:g} Hz, limit {r['limit']}" if "gain_db" in r
                  else r.get("why", ""))
        print(f"  {r['status']:11} {r['tag']:7} {r['id']}: {detail}")
        if args.verbose:
            for n in r.get("notes", []):
                print(f"      {n}")
    for p in rep["problems"]:
        print(f"  ! {p}")
    return 1 if rep["status"] == "FAIL" else 0


def cmd_datasheets(args):
    from boardsmith import datasheets as ds

    proj = Project.load(args.dir)
    try:
        if args.action == "status":
            rows = ds.status(proj)
            shown = [r for r in rows if r["wanted"] or args.all]
            print(f"{'MPN':28} {'refs':14} pdf extracted quality cache")
            for r in shown:
                refs = ",".join(r["refs"][:3]) + ("..." if len(r["refs"]) > 3 else "")
                ext = ("stale" if r["stale"] else "yes") if r["extracted"] else "-"
                print(f"{r['mpn'][:28]:28} {refs[:14]:14} {'yes' if r['pdf'] else '-':3} "
                      f"{ext:9} {r['quality'] if r['quality'] is not None else '-':7} "
                      f"{'yes' if r['in_cache'] else '-'}")
            no_pdf = sum(not r["pdf"] for r in shown)
            todo = sum(r["pdf"] and (not r["extracted"] or bool(r["stale"])) for r in shown)
            print(f"{len(shown)} parts ({len(rows) - len(shown)} passives etc. hidden; --all shows them): "
                  f"{no_pdf} without a PDF, {todo} to extract")
            return 0
        if args.action == "sync":
            res = ds.sync(proj, fresh=args.fresh, download=not args.offline)
        else:
            res = ds.publish(proj, force=args.force)
    except (FileNotFoundError, ValueError) as e:
        print(f"! {e}")
        return 1
    for k, v in res.items():
        print(f"  {k}: {len(v)}{' ' + ', '.join(v) if v else ''}")
    return 0


def cmd_status(args):
    proj = Project.load(args.dir)
    st = State(proj.state_file)
    for sid in STAGES:
        s = st.stage(sid)
        if s:
            print(f"[{sid}] {TITLES[sid]}: {s['status']} ({s.get('seconds')} s, {s.get('finished')}), "
                  f"{len(s.get('warnings', []))} warnings")
        else:
            print(f"[{sid}] {TITLES[sid]}: not run")
    return 0


def cmd_doctor(args):
    from boardsmith.kicad import Kicad, KicadError

    ok = True
    try:
        proj = Project.load(args.dir)
        cfg = proj.cfg
        print(f"config: {proj.root}/boardsmith.toml (design {cfg['design']['file']})")
    except (ValueError, FileNotFoundError) as e:
        print(f"config: {e} (checking machine settings only)")
        from boardsmith.config import DEFAULTS

        cfg = DEFAULTS
    try:
        k = Kicad.find(cfg["kicad"])
        print(f"kicad-cli: {k.cli} ({k.version})")
        print(f"KiCad python: {k.python}")
        print(f"symbols: {k.symbol_dir}\nfootprints: {k.footprint_dir}")
        for w in k.warnings:
            print(f"! {w}")
            ok = False
    except KicadError as e:
        print(f"! {e}")
        ok = False
    for name in cfg["routing"]["chain"]:
        good, msg = routers.get(name).available(cfg)
        print(f"router {name}: {'ok' if good else 'MISSING'} {msg}")
        ok &= good
    from boardsmith.review.spice_path import find_ngspice

    ng = find_ngspice(cfg.get("review", {}).get("ngspice", ""))
    print(f"ngspice: {ng or 'MISSING (the review gate reports SPICE checks as not checked)'}")
    try:
        f = fabs.get(cfg["fab"]["name"], cfg["fab"].get("tier"))
        print(f"fab: {f.name} tier {f.tier_name}")
    except ValueError as e:
        print(f"! {e}")
        ok = False
    # Look the packages up without importing them: importing SKiDL creates .log/.erc files in
    # the current directory.
    from importlib import metadata, util

    for pkg in ("skidl", "schematizer"):
        if util.find_spec(pkg) is None:
            print(f"! {pkg} is not installed")
            ok = False
            continue
        try:
            print(f"{pkg} {metadata.version(pkg)}")
        except metadata.PackageNotFoundError:
            print(f"{pkg} (version unknown)")
    import os
    import shutil

    cache = cfg.get("datasheets", {}).get("cache", "")
    print(f"datasheet cache: {os.path.expanduser(cache) if cache else '(none)'}")
    print(f"pdftotext: {shutil.which('pdftotext') or 'MISSING (downloaded PDFs are not identity-checked)'}")
    if util.find_spec("jsonschema") is None:
        print("jsonschema: MISSING (datasheets publish can't validate extractions; "
              "pip install 'boardsmith[analysis]')")
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(prog="boardsmith", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=__version__)
    ap.add_argument("-C", "--dir", default=".", help="project directory (has boardsmith.toml)")
    ap.add_argument("-v", "--verbose", action="store_true", help="full stage summaries")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run the physical pipeline")
    r.add_argument("--from", dest="start", default="c1", choices=STAGES)
    r.add_argument("--to", dest="end", default="c6", choices=STAGES)
    for alias, sid in ALIASES.items():
        sub.add_parser(alias, help=f"stage {sid}: {TITLES[sid]}")
    sub.add_parser("review", help="B4 gate: check constraints.json")
    d = sub.add_parser("datasheets", help="part-fact cache: status, sync, publish")
    d.add_argument("action", choices=("status", "sync", "publish"))
    d.add_argument("--all", action="store_true", help="status: include passives")
    d.add_argument("--fresh", action="store_true", help="sync: don't copy from the shared cache")
    d.add_argument("--offline", action="store_true", help="sync: don't download PDFs")
    d.add_argument("--force", action="store_true", help="publish: replace shared entries")
    sub.add_parser("status", help="show stage results")
    sub.add_parser("doctor", help="check the environment")
    args = ap.parse_args(argv)

    if args.cmd == "run":
        i, j = STAGES.index(args.start), STAGES.index(args.end)
        return cmd_run(args, STAGES[i:j + 1])
    if args.cmd in ALIASES:
        return cmd_run(args, (ALIASES[args.cmd],))
    if args.cmd == "status":
        return cmd_status(args)
    if args.cmd == "review":
        return cmd_review(args)
    if args.cmd == "datasheets":
        return cmd_datasheets(args)
    return cmd_doctor(args)


if __name__ == "__main__":
    sys.exit(main())
