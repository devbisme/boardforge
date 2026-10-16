"""Shared helpers for the board scripts (KiCad Python only)."""

import json
import math
import re
import sys
import traceback

import pcbnew

from boardforge.units import farads  # noqa: F401 - re-exported for the board scripts

MM = pcbnew.FromMM
TOMM = pcbnew.ToMM


def run(step):
    """Script entry point: load the job, run ``step(job) -> dict``, write the result."""
    with open(sys.argv[1]) as f:
        job = json.load(f)
    try:
        res = step(job) or {}
        res.setdefault("ok", True)
    except Exception as e:  # noqa: BLE001 - reported to the orchestrator
        res = {"ok": False, "error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()}
    with open(job["result"], "w") as f:
        json.dump(res, f, indent=1)


def pt(x, y):
    return pcbnew.VECTOR2I(MM(x), MM(y))


def xy(v):
    return TOMM(v.x), TOMM(v.y)


def rect(bb):
    return TOMM(bb.GetLeft()), TOMM(bb.GetTop()), TOMM(bb.GetRight()), TOMM(bb.GetBottom())


def edge_rect(board):
    return rect(board.GetBoardEdgesBoundingBox())


def load_footprint(fpid, fp_paths):
    """Load ``lib:name`` from the job's {nickname: .pretty path} map, with its LIB_ID set."""
    lib, name = fpid.split(":", 1)
    path = fp_paths.get(lib)
    fp = pcbnew.FootprintLoad(path, name) if path else None
    if fp is None:
        raise RuntimeError(f"footprint {fpid} not found ({path})")
    fp.SetFPID(pcbnew.LIB_ID(lib, name))
    return fp


def courtyard_rect(fp, margin=0.0):
    """Courtyard bbox on the footprint's side, else its full bbox (mm)."""
    try:
        cy = fp.GetCourtyard(pcbnew.B_CrtYd if fp.IsFlipped() else pcbnew.F_CrtYd)
        bb = cy.BBox() if cy.OutlineCount() else fp.GetBoundingBox(False)
    except Exception:  # noqa: BLE001 - some KiCad builds crash or lack courtyards
        bb = fp.GetBoundingBox(False)
    x0, y0, x1, y1 = rect(bb)
    return x0 - margin, y0 - margin, x1 + margin, y1 + margin


def overlap(a, b):
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def seg_dist(px, py, ax, ay, bx, by):
    vx, vy = bx - ax, by - ay
    L = vx * vx + vy * vy
    t = 0 if L == 0 else max(0, min(1, ((px - ax) * vx + (py - ay) * vy) / L))
    return math.hypot(px - ax - t * vx, py - ay - t * vy)


def dist_pt_rect(x, y, r):
    dx = max(r[0] - x, 0, x - r[2])
    dy = max(r[1] - y, 0, y - r[3])
    return math.hypot(dx, dy)


def add_zone(board, layer, net, outline_rect, clearance=0.2, priority=0):
    x0, y0, x1, y1 = outline_rect
    z = pcbnew.ZONE(board)
    z.SetLayer(layer)
    z.SetNet(net)
    ol = z.Outline()
    ol.NewOutline()
    for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
        ol.Append(MM(x), MM(y))
    z.SetAssignedPriority(priority)
    z.SetLocalClearance(MM(clearance))
    z.SetMinThickness(MM(0.2))
    z.SetPadConnection(pcbnew.ZONE_CONNECTION_THERMAL)
    z.SetThermalReliefGap(MM(0.25))
    z.SetThermalReliefSpokeWidth(MM(0.3))
    board.Add(z)
    return z


def layer_id(board, name):
    lid = board.GetLayerID(name)
    if lid < 0:
        raise ValueError(f"no layer {name!r}")
    return lid


def save(board, path):
    board.Save(path)
