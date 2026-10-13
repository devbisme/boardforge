"""The physical pipeline, C1-C6 (design doc §4.3).

Each stage reads files, writes files, records a short summary in pipeline_state.json and
returns it. Problems are warnings that are recorded; a stage only fails when the next stage
would have nothing to work on.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
import zipfile

from boardsmith import analysis, fabs, project, routers
from boardsmith.config import Project
from boardsmith.drc import drc, erc
from boardsmith.kicad import PKG_PARENT, Kicad
from boardsmith.netlist import compare, read_generic, read_kicad_netlist
from boardsmith.pcbfile import stable_uuids
from boardsmith.state import STAGES, State

RAIL_RX = re.compile(r"^([+-]?\d+V\d*\w*|V(CC|DD|EE|SS|BUS|IN|BAT|REF)\w*|\w*GND\w*|\w*VSS\w*)$", re.I)
GROUND_RX = re.compile(r"(GND|VSS)", re.I)


class StageError(RuntimeError):
    pass


class RunLock:
    """One pipeline run per build directory: a second one would mix up the board files."""

    def __init__(self, path):
        self.path = path

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        if os.path.exists(self.path):
            try:
                pid = int(open(self.path).read().strip() or 0)
                os.kill(pid, 0)
                raise StageError(f"another boardsmith run (pid {pid}) is using {os.path.dirname(self.path)}")
            except (ValueError, ProcessLookupError):
                pass  # stale lock
        with open(self.path, "w") as f:
            f.write(str(os.getpid()))
        return self

    def __exit__(self, *exc):
        if os.path.exists(self.path):
            os.remove(self.path)


class Pipeline:
    def __init__(self, proj: Project, kicad: Kicad = None):
        self.p = proj
        self.cfg = proj.cfg
        self.k = kicad or Kicad.find(self.cfg["kicad"])
        self.state = State(proj.state_file)
        self.fab = fabs.get(self.cfg["fab"]["name"], self.cfg["fab"].get("tier"))
        self.log = os.path.join(proj.reports, "kicad-cli.log")

    # ------------------------------------------------------------------ helpers
    # Each board stage starts from the previous stage's snapshot, so any stage can be rerun.
    @property
    def built_board(self):
        return os.path.join(self.p.work, "built.kicad_pcb")

    @property
    def placed_board(self):
        return os.path.join(self.p.work, "placed.kicad_pcb")

    def doc(self):
        return read_generic(self.p.netlist_json)

    def net_map(self):
        if not os.path.exists(self.p.net_map_file):
            raise StageError("no net map: run C1 first")
        with open(self.p.net_map_file) as f:
            return json.load(f)

    def knet(self, name, nm=None, warnings=None):
        """KiCad name of a SKiDL net (config uses SKiDL names); unmapped names pass through."""
        nm = nm if nm is not None else self.net_map()
        if name in nm:
            return nm[name]
        if name in nm.values():
            return name
        if warnings is not None:
            warnings.append(f"net {name!r} not in the design")
        return name

    def classes(self):
        """(classes, {kicad net: [class]}, warnings) from the SKiDL net classes."""
        return project.netclasses_from_doc(self.doc(), self.net_map(), self.cfg["rules"])

    def write_pro(self):
        classes, assign, _ = self.classes()
        project.write_netclasses(self.p.kfile(".kicad_pro"), classes, assign, self.cfg["rules"])

    def rails(self):
        """(rails, ground) as KiCad names: plane nets, power-class nets, power-like names."""
        nm = self.net_map()
        doc = self.doc()
        power_classes = {n for n in doc.get("netclasses", {}) if "power" in n.lower()}
        rails, ground = set(), set()
        planes = set(self.cfg["board"]["planes"].values())
        for n in doc["nets"]:
            name = n["name"]
            if name not in nm:
                continue
            tail = name.split("/")[-1]
            cls = set(n.get("attrs", {}).get("netclasses", []))
            if name in planes or cls & power_classes or RAIL_RX.match(tail):
                rails.add(nm[name])
                if GROUND_RX.search(tail):
                    ground.add(nm[name])
        return sorted(rails), sorted(ground)

    def fp_paths(self):
        comps, _ = read_kicad_netlist(self.p.kfile(".net"))
        libs = {c["footprint"].split(":")[0] for c in comps.values() if ":" in c["footprint"]}
        b = self.cfg["board"]
        for extra in (b["mounting_holes"].get("footprint", ""), "Fiducial:Fiducial_1mm_Mask2mm"):
            if ":" in extra:
                libs.add(extra.split(":")[0])
        return project.footprint_paths(libs, self.k.footprint_dir,
                                       self.cfg["libraries"]["footprints"])

    def plane_nets(self, warnings=None):
        nm = self.net_map()
        return {layer: self.knet(net, nm, warnings) for layer, net in self.cfg["board"]["planes"].items()}

    def run_stage(self, sid, fn):
        t0 = time.time()
        try:
            status, summary, warnings = fn()
        except Exception as e:  # noqa: BLE001 - recorded in the state file
            status, summary, warnings = "failed", {"error": f"{type(e).__name__}: {e}"}, []
            with open(os.path.join(self.p.reports, f"{sid}_error.txt"), "w") as f:
                f.write(traceback.format_exc())
        secs = round(time.time() - t0, 1)
        self.state.record(sid, status, summary, warnings, secs)
        return {"stage": sid, "status": status, "seconds": secs, "summary": summary,
                "warnings": warnings}

    # ------------------------------------------------------------------ C1
    def c1(self):
        """SKiDL -> netlist JSON + labels-only schematic -> kicad-cli netlist -> parity + ERC."""
        d = self.cfg["design"]
        kdir = self.p.kicad_dir
        os.makedirs(kdir, exist_ok=True)
        for f in os.listdir(kdir):  # stale sheets from an earlier hierarchy
            if f.endswith(".kicad_sch"):
                os.remove(os.path.join(kdir, f))
        job = {"design": d["file"], "top": d["top"], "name": self.p.name, "title": d["title"],
               "symbol_dir": self.k.symbol_dir, "out_json": self.p.netlist_json, "sch_dir": kdir,
               "result": os.path.join(self.p.work, "skidl_result.json")}
        jf = os.path.join(self.p.work, "skidl_job.json")
        with open(jf, "w") as f:
            json.dump(job, f, indent=1)
        env = dict(os.environ, PYTHONPATH=PKG_PARENT + os.pathsep + os.environ.get("PYTHONPATH", ""))
        r = subprocess.run([sys.executable, "-m", "boardsmith.skidl_runner", jf], capture_output=True,
                           text=True, env=env, timeout=3600,
                           cwd=self.p.work)  # SKiDL opens its .log/.erc in the cwd on import
        with open(os.path.join(self.p.reports, "skidl.log"), "w") as f:
            f.write(r.stdout + r.stderr)
        if not os.path.exists(job["result"]):
            raise StageError(f"SKiDL run crashed: {r.stderr.strip()[-500:]}")
        with open(job["result"]) as f:
            sk = json.load(f)
        if not sk.get("ok"):
            raise StageError(f"SKiDL run failed: {sk.get('error')}")
        warnings = list(sk.get("warnings", []))

        fp_paths = project.footprint_paths(sk["fp_libs"], self.k.footprint_dir,
                                           self.cfg["libraries"]["footprints"])
        missing = project.write_lib_tables(kdir, sk["sym_libs"], fp_paths, self.k.symbol_dir)
        warnings += [f"footprint library {m} not found" for m in missing]

        sch = self.p.kfile(".kicad_sch")
        net = self.p.kfile(".net")
        self.k.cli_run("sch", "export", "netlist", "--format", "kicadsexpr", "-o", net, sch,
                       log=self.log, check=True)
        _, knets = read_kicad_netlist(net)
        par = compare(self.doc(), knets)
        with open(os.path.join(self.p.reports, "parity.json"), "w") as f:
            json.dump(par, f, indent=1)
        with open(self.p.net_map_file, "w") as f:
            json.dump(par["net_map"], f, indent=1)
        if not par["ok"]:
            warnings.append(f"netlist parity: {len(par['missing'])} SKiDL nets and "
                            f"{len(par['extra'])} KiCad nets don't match (reports/parity.json)")

        classes, assign, w = self.classes()
        warnings += w
        project.write_netclasses(self.p.kfile(".kicad_pro"), classes, assign, self.cfg["rules"])

        e = erc(self.k, sch, os.path.join(self.p.reports, "erc.json"), log=self.log)
        if e.get("errors"):
            warnings.append(f"ERC: {e['errors']} errors (reports/erc.json)")
        summary = {
            "components": sk["components"], "nets": sk["nets"], "sheets": sk["sheets"],
            "skidl_erc": {"errors": sk["erc_errors"], "warnings": sk["erc_warnings"]},
            "parity": {k: par[k] for k in ("ok", "skidl_nets", "kicad_nets", "matched")}
                      | {"details": par["details"][:5]},
            "netclasses": {c["name"]: sum(1 for v in assign.values() if c["name"] in v) for c in classes},
            "erc": {"errors": e.get("errors"), "by_type": e.get("by_type"),
                    "error_items": e.get("error_items", [])[:5]},
        }
        status = "done" if par["ok"] and not e.get("errors") else "warn"
        return status, summary, warnings

    # ------------------------------------------------------------------ C2
    def board_rules(self, warnings):
        b = self.cfg["board"]
        mins, notes = self.fab.design_rules(b["layers"])
        warnings += notes
        rules = dict(self.cfg["rules"])
        warnings += self.fab.check_rules(rules)
        classes, _, _ = self.classes()
        for c in classes:
            warnings += [f"net class {c['name']}: {w}" for w in self.fab.check_rules(c)]
        # The board minimums are the smallest values a net class uses (not the fab's limits), so
        # DRC flags copper a router thinned below the design's own widths; check_rules above
        # already warns about classes below the fab's limits.
        rules["min_track"] = max(mins["min_track"], min([rules["track_width"]] + [c["track_width"] for c in classes]))
        rules["min_clearance"] = max(mins["min_clearance"],
                                     min([rules["clearance"]] + [c["clearance"] for c in classes]))
        rules["min_via_diameter"] = mins["min_via_diameter"]
        rules["min_drill"] = mins["min_drill"]
        rules["hole_clearance"] = mins["hole_clearance"]
        rules["edge_clearance"] = max(mins["edge_clearance"], rules.get("edge_clearance", 0))
        return rules

    def c2(self, size=None, fill=None):
        """Board from the KiCad netlist: stackup and rules from the fab, nets, footprints, planes."""
        warnings = []
        b = self.cfg["board"]
        rules = self.board_rules(warnings)
        nm = self.net_map()
        mh = dict(b["mounting_holes"])
        if mh.get("net"):
            mh["net"] = self.knet(mh["net"], nm, warnings)
        job = {"netlist": self.p.kfile(".net"), "pcb": self.p.kfile(".kicad_pcb"),
               "fp_paths": self.fp_paths(), "size": size or b["size"], "fill": fill or b["fill"],
               "origin": b["origin"], "layers": b["layers"], "thickness": b["thickness"],
               "planes": self.plane_nets(warnings), "rules": rules, "mounting_holes": mh,
               "title": self.cfg["design"]["title"] or self.p.name, "revision": b["revision"],
               "date": time.strftime("%Y-%m-%d")}
        r = self.k.pcb("build", job, self.p.work)
        if not r.get("ok"):
            raise StageError(r.get("error"))
        self.write_pro()
        routers.copy_board(self.p.kfile(".kicad_pcb"), self.built_board)
        warnings += r.get("warnings", [])
        summary = {k: r[k] for k in ("footprints", "nets", "size", "size_estimated")}
        summary.update(layers=b["layers"], fab=f"{self.fab.name}/{self.fab.tier_name}",
                       min_track=rules["min_track"], min_clearance=rules["min_clearance"])
        return ("warn" if warnings else "done"), summary, warnings

    # ------------------------------------------------------------------ C3
    def c3(self):
        warnings = []
        pl = self.cfg["placement"]
        rails, ground = self.rails()
        if not os.path.exists(self.built_board):
            raise StageError("no board: run C2 first")
        routers.copy_board(self.built_board, self.p.kfile(".kicad_pcb"))
        job = {"pcb": self.p.kfile(".kicad_pcb"), "anchors": pl["anchor"],
               "plane_nets": list(self.plane_nets(warnings).values()), "rails": rails,
               "ground_nets": ground, "bottom_decap_owners": pl["bottom_decap_owners"],
               "keepout": self.cfg["rules"]["edge_clearance"] + 0.2}
        r = self.k.pcb("place", job, self.p.work)
        if "placed" not in r:
            raise StageError(r.get("error"))
        self.write_pro()
        routers.copy_board(self.p.kfile(".kicad_pcb"), self.placed_board)
        warnings += r.get("warnings", [])
        summary = {k: r.get(k) for k in ("anchored", "placed", "bottom_side", "unplaced")}
        if r.get("unplaced"):
            return "warn", summary, warnings + [f"{len(r['unplaced'])} parts could not be placed"]
        return ("warn" if warnings else "done"), summary, warnings

    # ------------------------------------------------------------------ C4
    def route_job(self, board, workdir, warnings):
        rt = self.cfg["routing"]
        rules = self.cfg["rules"]
        classes, assign, _ = self.classes()
        by_name = {c["name"]: c for c in classes}
        net_rules = {}
        for net, cs in assign.items():
            c = min((by_name[x] for x in cs if x in by_name), key=lambda c: c["priority"], default=None)
            if c:
                net_rules[net] = (c["track_width"], c["clearance"])
        planes = self.plane_nets(warnings)
        roles = {f"In{i}.Cu": "signal" for i in range(1, self.cfg["board"]["layers"] - 1)}
        roles.update({layer: "plane" for layer in planes})
        return routers.RouteJob(
            board=board, workdir=workdir, kicad=self.k, plane_nets=sorted(set(planes.values())),
            layer_roles=roles, default_rule=(rules["track_width"], rules["clearance"]),
            net_rules=net_rules, via=(rules["via_diameter"], rules["via_drill"]),
            project_file=self.p.kfile(".kicad_pro"),
            time_budget=rt["freerouting"].get("timeout", 3600),
            threads=rt["freerouting"].get("threads", 1))

    def _fanout(self, board, job):
        r = self.k.pcb("fanout", {"pcb": board, "plane_nets": job.plane_nets,
                                  "via_diameter": job.via[0], "via_drill": job.via[1],
                                  "clearance": job.default_rule[1],
                                  "edge_clearance": self.cfg["rules"]["edge_clearance"] + 0.2},
                       self.p.work)
        if not r.get("ok"):
            raise StageError(f"fanout: {r.get('error')}")
        return r

    def c4(self):
        warnings = []
        rt = self.cfg["routing"]
        wd = os.path.join(self.p.work, "route")
        if os.path.isdir(wd):
            shutil.rmtree(wd)
        os.makedirs(wd)
        if not os.path.exists(self.placed_board):
            raise StageError("no placed board: run C3 first")
        board = routers.copy_board(self.placed_board, os.path.join(wd, "board.kicad_pcb"))
        job = self.route_job(board, wd, warnings)
        steps = {}

        if job.plane_nets:
            r = self._fanout(board, job)
            steps["fanout"] = {"pads": r["pads"], "vias": r["vias"], "failed": len(r["failed"])}
            if r["failed"]:
                warnings.append(f"fanout failed for {len(r['failed'])} pads: {r['failed'][:6]}")

        if rt["preroute"]:
            nm = self.net_map()
            nets = [self.knet(n, nm, warnings) for n in rt["preroute"]]
            r = self.k.pcb("astar", {"mode": "preroute", "pcb": board, "nets": nets,
                                     "width": rt["preroute_width"], "plane_nets": job.plane_nets,
                                     "via_diameter": job.via[0], "via_drill": job.via[1],
                                     "default_rule": list(job.default_rule),
                                     "net_rules": {n: list(v) for n, v in job.net_rules.items()}},
                           self.p.work)
            steps["preroute"] = r.get("nets", r.get("error"))
            warnings += [f"preroute: {f}" for f in r.get("failed", [])]

        stable_uuids(board)  # same board -> same Specctra file -> same Freerouting run
        chain = []
        for i, name in enumerate(rt["chain"]):
            router = routers.get(name)
            job.options = rt.get(name, {})
            if i > 0 and job.plane_nets:
                self._fanout(job.board, job)  # plane pads the previous router left bare
            if i > 0:
                stable_uuids(job.board)
            res = router.route(job)
            chain.append({"router": name, "ok": res.ok, "unrouted": res.unrouted,
                          "stats": res.stats, **({"error": res.error} if res.error else {})})
            if not res.ok:
                warnings.append(f"router {name} failed: {res.error}")
                continue
            job.board = res.board
            job.unrouted = res.unrouted_items
            if res.unrouted == 0 and not res.stats.get("drc_errors"):
                break  # otherwise the next router (the A* finisher) repairs the errors
        steps["chain"] = chain

        final = self.p.kfile(".kicad_pcb")
        shutil.copyfile(job.board, final)
        b = self.cfg["board"]
        before = os.path.join(wd, "before_cleanup.kicad_pcb")
        shutil.copyfile(final, before)
        r = self.k.pcb("finish", {"pcb": final, "phase": "cleanup", "plane_nets": job.plane_nets},
                       self.p.work)
        dangling = r.get("dangling_removed")
        if not r.get("ok"):
            warnings.append(f"cleanup failed: {r.get('error')}")
        elif dangling:
            # Guard: removing leftovers must never disconnect anything the routers connected.
            self.write_pro()
            n_before = chain[-1]["unrouted"] if chain and chain[-1]["ok"] else None
            n_after = drc(self.k, final, os.path.join(wd, "cleanup_drc.json")).get("unconnected")
            if n_before is not None and n_after is not None and n_after > n_before:
                shutil.copyfile(before, final)
                warnings.append(f"cleanup disconnected copper ({n_before} -> {n_after} unconnected); "
                                f"kept the board without cleanup")
                dangling = 0
        r = self.k.pcb("finish", {"pcb": final, "pours": [self.knet(n, None, warnings) for n in b["pours"]],
                                  "min_track": self.board_rules([])["min_track"],
                                  "plane_nets": job.plane_nets, "stitch_pitch": b["stitch_pitch"],
                                  "via_diameter": job.via[0], "via_drill": job.via[1],
                                  "clearance": job.default_rule[1],
                                  "edge_clearance": self.cfg["rules"]["edge_clearance"]}, self.p.work)
        if not r.get("ok"):
            warnings.append(f"finish failed: {r.get('error')}")
        steps["finish"] = {"dangling_removed": dangling,
                           **{k: r.get(k) for k in ("widened", "pours_added", "stitching_vias")}}
        if b["fiducials"]:
            r = self.k.pcb("fiducials", {"pcb": final, "fp_paths": self.fp_paths(),
                                         "footprint": "Fiducial:Fiducial_1mm_Mask2mm",
                                         "label": f"{self.p.name} Rev {b['revision']}"},
                           self.p.work)
            steps["fiducials"] = len(r.get("placed", []))
            if not r.get("label"):
                warnings.append("no room for the board name/revision label")
            warnings += [f"fiducial not placed: {f}" for f in r.get("failed", [])]
        self.write_pro()
        rep = drc(self.k, final, os.path.join(self.p.reports, "drc_route.json"))
        summary = {"unrouted": rep.get("unconnected"), "drc_errors": rep.get("errors"), **steps}
        if rep.get("unconnected"):
            warnings.append(f"{rep['unconnected']} connections unrouted")
        return ("warn" if warnings else "done"), summary, warnings

    # ------------------------------------------------------------------ C5
    def c5(self):
        warnings = []
        ck = self.cfg["checks"]
        pcb = self.p.kfile(".kicad_pcb")
        rep = drc(self.k, pcb, os.path.join(self.p.reports, "drc.json"), parity=True, log=self.log)
        rails, ground = self.rails()
        m = self.k.pcb("measure", {"pcb": pcb, "rails": rails, "ground_nets": ground,
                                   "decap_max_mm": ck["decap_max_mm"],
                                   "length_nets": [self.knet(n, None, warnings)
                                                   for n in ck.get("length_nets", [])]},
                       self.p.work)
        with open(os.path.join(self.p.reports, "geometry.json"), "w") as f:
            json.dump(m, f, indent=1)
        dfm = self.fab.dfm_checks(m)
        currents = {self.knet(n, None, warnings): a for n, a in ck.get("rail_currents", {}).items()}
        ctx = analysis.Context(self.p.kfile(".kicad_sch"), pcb, os.path.join(self.p.reports, "analysis"),
                               rail_currents=currents)
        an = analysis.run_steps(ctx, ck["analyzers"], ck.get("steps"))
        unused = set(currents) - {rail for _, rail, _ in ctx.loads}
        if unused and {"thermal", "cross_verify"} & set(ctx.outputs):
            warnings.append(f"checks.rail_currents: no regulator outputs {sorted(unused)}")
        with open(os.path.join(self.p.reports, "analysis_outputs.json"), "w") as f:
            json.dump(ctx.outputs, f, indent=1)

        if rep.get("errors"):
            warnings.append(f"DRC: {rep['errors']} errors")
        if rep.get("unconnected"):
            warnings.append(f"DRC: {rep['unconnected']} unconnected")
        if rep.get("parity"):
            warnings.append(f"DRC: {rep['parity']} schematic-parity issues")
        if m.get("decap_far"):
            warnings.append(f"{len(m['decap_far'])} decaps farther than {ck['decap_max_mm']} mm "
                            f"from their IC pin (reports/geometry.json)")
        warnings += dfm
        warnings += [f"analysis {s}: {v['error']}" for s, v in an.items() if "error" in v]
        summary = {
            "drc": {k: rep.get(k) for k in ("errors", "warnings", "unconnected", "parity")}
                   | {"error_items": rep.get("error_items", [])[:5]},
            "board": {k: m.get(k) for k in ("board_mm", "copper_layers", "parts_top", "parts_bottom",
                                            "track_mm", "vias")},
            "decaps": {"checked": m.get("decaps_checked"), "far": m.get("decap_far", [])[:8],
                       "far_bulk": len(m.get("decap_far_bulk", []))},
            "net_lengths_mm": m.get("net_lengths_mm"),
            "dfm": dfm,
            "analysis": an,
            "rail_loads": {f"{ref} {rail}": amps for ref, rail, amps in ctx.loads},
        }
        ok = not rep.get("errors") and not rep.get("unconnected") and not rep.get("parity")
        return ("done" if ok and not warnings else "warn"), summary, warnings

    # ------------------------------------------------------------------ C6
    def c6(self):
        warnings = []
        name = self.p.name
        pcb, sch = self.p.kfile(".kicad_pcb"), self.p.kfile(".kicad_sch")
        fab_dir = os.path.join(self.p.out, "fab")
        if os.path.isdir(fab_dir):
            shutil.rmtree(fab_dir)
        gdir = os.path.join(fab_dir, "gerbers")
        os.makedirs(gdir)
        docs = os.path.join(self.p.out, "docs")
        os.makedirs(docs, exist_ok=True)
        k, log = self.k, self.log
        layers = self.fab.gerber_layers(self.cfg["board"]["layers"])
        k.cli_run("pcb", "export", "gerbers", "--layers", ",".join(layers), "-o", gdir + "/", pcb,
                  log=log, check=True)
        k.cli_run("pcb", "export", "drill", *self.fab.drill_args(), "-o", gdir + "/", pcb,
                  log=log, check=True)
        zpath = os.path.join(fab_dir, f"{name}-gerbers.zip")
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
            for f in sorted(os.listdir(gdir)):
                z.write(os.path.join(gdir, f), f)

        pos = os.path.join(self.p.work, "pos.csv")
        k.cli_run("pcb", "export", "pos", "--format", "csv", "--units", "mm", "--side", "both",
                  "--exclude-dnp", "-o", pos, pcb, log=log, check=True)
        kbom = os.path.join(self.p.work, "bom.csv")
        k.cli_run("sch", "export", "bom", "--fields",
                  "Reference,Value,Footprint,MPN,Manufacturer,LCSC,${QUANTITY},${DNP}",
                  "--labels", "Reference,Value,Footprint,MPN,Manufacturer,LCSC,Quantity,DNP",
                  "--group-by", "Value,Footprint,MPN,LCSC", "--exclude-dnp", "-o", kbom, sch,
                  log=log, check=True)
        comps, _ = read_kicad_netlist(self.p.kfile(".net"))
        fields = {r: c["fields"] for r, c in comps.items()}
        fps = {r: c["footprint"] for r, c in comps.items()}
        bom = os.path.join(fab_dir, f"{name}-bom-{self.fab.name}.csv")
        cpl = os.path.join(fab_dir, f"{name}-cpl-{self.fab.name}.csv")
        bres = self.fab.bom(kbom, bom, fields)
        cres = self.fab.cpl(pos, cpl, fps, bom_csv=bom)
        if bres.get("lines_without_lcsc"):
            warnings.append(f"{bres['lines_without_lcsc']} BOM lines have no LCSC number "
                            "(set part.fields['LCSC'])")
        if cres.get("rotation_adjusted"):
            warnings.append(f"rotation offsets applied to {len(cres['rotation_adjusted'])} parts "
                            "from an unverified table; check the fab's placement preview")

        k.cli_run("sch", "export", "pdf", "-o", os.path.join(docs, f"{name}_schematic.pdf"), sch, log=log)
        renders = []
        if self.cfg["fab"].get("renders"):
            for side in ("top", "bottom"):
                out = os.path.join(docs, f"pcb_3d_{side}.png")
                k.cli_run("pcb", "render", "--side", side, "--width", "1600", "--height", "1100",
                          "--quality", "basic", "-o", out, pcb, log=log)
                if os.path.exists(out):
                    renders.append(os.path.basename(out))

        ctx = analysis.Context(sch, pcb, os.path.join(self.p.reports, "analysis"), gerber_dir=gdir,
                               drc_report=os.path.join(self.p.reports, "drc.json"))
        prev = os.path.join(self.p.reports, "analysis_outputs.json")
        if os.path.exists(prev):
            with open(prev) as f:
                ctx.outputs.update(json.load(f))
        an = analysis.run_steps(ctx, ["gerbers", "release_gate"], self.cfg["checks"].get("steps"))
        warnings += [f"analysis {s}: {v['error']}" for s, v in an.items() if "error" in v]
        summary = {"gerbers": len(os.listdir(gdir)), "zip": os.path.relpath(zpath, self.p.out),
                   "bom": os.path.relpath(bom, self.p.out),
                   "bom_lines": bres.get("lines"), "cpl": os.path.relpath(cpl, self.p.out),
                   "placements": cres.get("placements"), "renders": renders, "analysis": an}
        return ("warn" if warnings else "done"), summary, warnings

    # ------------------------------------------------------------------ pipeline
    def run(self, stages=STAGES, retries=None):
        """Run stages in order. If routing leaves connections and the board size was estimated,
        loosen the fill factor and redo C2-C4 (up to ``routing.grow_retries`` times)."""
        with RunLock(os.path.join(self.p.out, ".lock")):
            return self._run(stages, retries)

    def _run(self, stages, retries):
        out = []
        fns = {"c1": self.c1, "c2": self.c2, "c3": self.c3, "c4": self.c4, "c5": self.c5, "c6": self.c6}
        retries = self.cfg["routing"].get("grow_retries", 1) if retries is None else retries
        fill = self.cfg["board"]["fill"]
        for sid in stages:
            res = self.run_stage(sid, fns[sid])
            out.append(res)
            while (sid == "c4" and res["summary"].get("unrouted") and retries > 0 and "c2" in stages
                   and self.state.stage("c2").get("summary", {}).get("size_estimated")):
                retries -= 1
                fill *= 0.85
                out.append(self.run_stage("c2", lambda: self.c2(fill=fill)))
                out.append(self.run_stage("c3", self.c3))
                res = self.run_stage("c4", self.c4)
                out.append(res)
            if res["status"] == "failed":
                break
        return out
