"""Router interface (design doc §4.6). The pipeline never names a router; it builds a
RouteJob, asks each router in the configured chain to route it, and reads RouteResults."""

import os
import shutil
from dataclasses import dataclass, field

from boardforge.drc import drc


@dataclass
class RouteJob:
    """Router-neutral input. Net names are KiCad's."""

    board: str  # scratch copy; prerouted and fanned-out copper is locked
    workdir: str
    kicad: object  # boardforge.kicad.Kicad
    plane_nets: list = field(default_factory=list)  # treated as plane-connected, not routed
    layer_roles: dict = field(default_factory=dict)  # {layer: "signal" | "plane" | "avoid"}
    default_rule: tuple = (0.15, 0.15)  # (width, clearance) mm
    net_rules: dict = field(default_factory=dict)  # {net: (width, clearance)}
    via: tuple = (0.45, 0.2)  # (diameter, drill) mm
    diff_pairs: list = field(default_factory=list)  # [(p, n)]; no router uses them yet
    time_budget: float = 3600.0  # s
    threads: int = 1
    seed: int = 0
    options: dict = field(default_factory=dict)  # router-specific config section
    unrouted: list = field(default_factory=list)  # previous router's leftovers
    project_file: str = ""  # canonical .kicad_pro (rules, net classes) for DRC on scratch boards


@dataclass
class RouteResult:
    board: str
    unrouted: int
    unrouted_items: list
    stats: dict
    log: str
    ok: bool = True
    error: str = ""


class Router:
    """Base class. Subclasses set ``name`` and implement ``route``."""

    name = "base"

    def capabilities(self):
        """Features the router handles itself; the pipeline does the rest (e.g. plane fanout)."""
        return {"planes": False, "diff_pairs": False, "length_matching": False,
                "incremental": False, "multithreaded": False}

    def available(self, cfg):
        """(ok, message) for ``boardforge doctor``."""
        return True, ""

    def route(self, job: RouteJob) -> RouteResult:
        raise NotImplementedError

    # ---- helpers for subclasses
    def _measure(self, job, board, extra_stats=None, log=""):
        """Unrouted connections on ``board`` according to KiCad's DRC."""
        if job.project_file:  # pcbnew's Save() drops net classes from the scratch .kicad_pro
            shutil.copyfile(job.project_file, os.path.splitext(board)[0] + ".kicad_pro")
        rep = drc(job.kicad, board, os.path.join(job.workdir, f"{self.name}_drc.json"))
        stats = {"drc_errors": rep.get("errors"), **(extra_stats or {})}
        return RouteResult(board=board, unrouted=rep.get("unconnected", -1),
                           unrouted_items=rep.get("unconnected_items", []), stats=stats, log=log,
                           ok=rep.get("ok", False), error=rep.get("error", ""))


def copy_board(src, dst):
    shutil.copyfile(src, dst)
    pro = os.path.splitext(src)[0] + ".kicad_pro"
    if os.path.exists(pro):
        shutil.copyfile(pro, os.path.splitext(dst)[0] + ".kicad_pro")
    return dst
