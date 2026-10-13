"""The internal grid A* router (boardsmith.pcb.astar) as a Router.

Used as the finisher. Each pass: DRC; if the previous router left repairable errors (necked,
too-close or crossing copper), remove that copper; otherwise route the unconnected pairs
(pass 0 without rip-up, later passes with cost-based rip-up). The board with the fewest
unrouted connections plus DRC errors is kept. Rip-up history carries across passes: nets
ripped before cost more to rip again, and pairs that already forced rip-ups go last, which
stops two nets ripping each other up in turn.
"""

import collections
import json
import os
import shutil

from boardsmith.drc import drc, repairable
from boardsmith.pcbfile import stable_uuids
from boardsmith.routers.base import Router, RouteResult, copy_board


def _repairable(report_file):
    with open(report_file) as f:
        return repairable(json.load(f))


class AStar(Router):
    name = "astar"

    def capabilities(self):
        return dict(super().capabilities(), incremental=True)

    def route(self, job):
        k = job.kicad
        out = os.path.join(job.workdir, "astar_routed.kicad_pcb")
        best = os.path.join(job.workdir, "astar_best.kicad_pcb")
        copy_board(job.board, out)
        base = {
            "pcb": out, "plane_nets": job.plane_nets,
            "via_diameter": job.via[0], "via_drill": job.via[1],
            "default_rule": list(job.default_rule),
            "net_rules": {n: list(r) for n, r in job.net_rules.items()},
            "max_expansions": job.options.get("max_expansions", 600000),
        }
        report = os.path.join(job.workdir, "astar_drc.json")
        passes = int(job.options.get("passes", 10))
        repairs_left = int(job.options.get("repairs", 3))
        best_score, history, removed = None, [], 0
        rip_history, attempts = collections.Counter(), collections.Counter()
        i = 0
        while True:
            if job.project_file:
                shutil.copyfile(job.project_file, os.path.splitext(out)[0] + ".kicad_pro")
            rep = drc(k, out, report)
            n = rep.get("unconnected", -1)
            bad = _repairable(report) if rep.get("ok") else []
            score = n + rep.get("errors", 0) if n >= 0 else None
            history.append(n if not bad else f"{n}+{len(bad)}err")
            if score is not None and (best_score is None or score < best_score):
                best_score = score
                shutil.copyfile(out, best)
            if bad and repairs_left > 0:
                repairs_left -= 1
                r = k.pcb("astar", dict(base, mode="repair", drc=report), job.workdir)
                if not r.get("ok"):
                    return RouteResult(best, n, [], {"history": history}, "", ok=False,
                                       error=r.get("error", "astar repair failed"))
                removed += r.get("removed", 0)
                stable_uuids(out)  # each pass sees the board in the same order every run
                if r.get("removed"):
                    continue  # re-DRC: the removed copper shows up as unconnected pairs
            if n <= 0 or i >= passes:
                break
            # Pass 0 routes without rip-up; later passes may rip up crossed tracks.
            r = k.pcb("astar", dict(base, mode="fix", drc=report, ripup=i > 0,
                                    rip_history=dict(rip_history), attempts=dict(attempts)),
                      job.workdir)
            if not r.get("ok"):
                return RouteResult(best, n, [], {"history": history}, "", ok=False,
                                   error=r.get("error", "astar failed"))
            stable_uuids(out)
            for rip in r.get("ripped", []):
                attempts[rip["for"]] += 1
                rip_history.update(rip["nets"])
            i += 1
        shutil.copyfile(best, out)
        return self._measure(job, out, {"history": history, "ripups": sum(attempts.values()),
                                        "repair_removed": removed})
