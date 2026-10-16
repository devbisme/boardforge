"""C3 routability estimate: RUDY (rectangular uniform wire density) on a grid.

Each signal net is split into the edges of a minimum spanning tree over its pads. An edge
needs about (|dx| + |dy|) * pitch of routing area, spread evenly over its bounding box. A
cell's demand divided by its capacity (cell area times the signal layers) is its
utilisation; plane nets are left out (they connect by vias). Pads block part of the outer
layers, so their copper is added to the demand of the cells they sit in.

Job: pcb, plane_nets, pitch (track + clearance, mm), signal_layers, cell (mm), limit.
Result: max, p95 and mean utilisation, cells over 1.0 and over ``limit``, a histogram (cells
over 0.3 ... 0.9), the hottest cells, ratsnest crossings and length.
"""

import math

import pcbnew

from boardforge.pcb.common import TOMM, edge_rect, run, xy


def _mst(pts):
    """Prim's MST over points; returns index pairs."""
    n = len(pts)
    if n < 2:
        return []
    inside = [False] * n
    best = [(math.inf, -1)] * n
    best[0] = (0, -1)
    edges = []
    for _ in range(n):
        i = min((k for k in range(n) if not inside[k]), key=lambda k: best[k][0])
        inside[i] = True
        if best[i][1] >= 0:
            edges.append((best[i][1], i))
        for k in range(n):
            if not inside[k]:
                d = abs(pts[i][0] - pts[k][0]) + abs(pts[i][1] - pts[k][1])
                if d < best[k][0]:
                    best[k] = (d, i)
    return edges


def _crossings(segs):
    def o(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    n = 0
    for i, (a, b, na) in enumerate(segs):
        for c, d, nc in segs[i + 1:]:
            if na != nc and o(a, b, c) * o(a, b, d) < 0 and o(c, d, a) * o(c, d, b) < 0:
                n += 1
    return n


def congestion(job):
    board = pcbnew.LoadBoard(job["pcb"])
    planes = set(job.get("plane_nets", []))
    pitch = job.get("pitch", 0.3)
    layers = job.get("signal_layers", 2)
    g = job.get("cell", 2.5)
    x0, y0, x1, y1 = edge_rect(board)
    nx, ny = max(1, math.ceil((x1 - x0) / g)), max(1, math.ceil((y1 - y0) / g))
    demand = [[0.0] * nx for _ in range(ny)]

    def spread(ax, ay, bx, by, amount):
        lx, hx, ly, hy = min(ax, bx), max(ax, bx), min(ay, by), max(ay, by)
        # at least one track pitch wide, so straight edges still occupy area
        if hx - lx < pitch:
            lx, hx = (lx + hx - pitch) / 2, (lx + hx + pitch) / 2
        if hy - ly < pitch:
            ly, hy = (ly + hy - pitch) / 2, (ly + hy + pitch) / 2
        area = (hx - lx) * (hy - ly)
        for j in range(max(0, int((ly - y0) / g)), min(ny, int((hy - y0) / g) + 1)):
            cy0, cy1 = y0 + j * g, y0 + (j + 1) * g
            oy = min(hy, cy1) - max(ly, cy0)
            if oy <= 0:
                continue
            for i in range(max(0, int((lx - x0) / g)), min(nx, int((hx - x0) / g) + 1)):
                cx0, cx1 = x0 + i * g, x0 + (i + 1) * g
                ox = min(hx, cx1) - max(lx, cx0)
                if ox > 0:
                    demand[j][i] += amount * ox * oy / area

    nets = {}
    for fp in board.GetFootprints():
        for p in fp.Pads():
            n = p.GetNetname()
            px, py = xy(p.GetPosition())
            if p.GetAttribute() == pcbnew.PAD_ATTRIB_SMD:  # blocks one outer layer
                bb = p.GetBoundingBox()
                w, h = TOMM(bb.GetWidth()), TOMM(bb.GetHeight())
                spread(px - w / 2, py - h / 2, px + w / 2, py + h / 2, w * h)
            if n and n not in planes and not n.startswith("unconnected"):
                nets.setdefault(n, []).append((px, py))
    segs, length = [], 0.0
    for n, pts in sorted(nets.items()):
        for a, b in _mst(pts):
            (ax, ay), (bx, by) = pts[a], pts[b]
            L = abs(ax - bx) + abs(ay - by)
            length += L
            spread(ax, ay, bx, by, L * pitch)
            segs.append((pts[a], pts[b], n))
    cap = g * g * layers
    util = sorted((demand[j][i] / cap, j, i) for j in range(ny) for i in range(nx))
    vals = [u for u, _, _ in util]
    hot = [{"at": [round(i * g + g / 2, 1), round(j * g + g / 2, 1)], "util": round(u, 2)}
           for u, j, i in util[::-1][:8]]
    return {"cell_mm": g, "max": round(vals[-1], 2), "p95": round(vals[int(0.95 * (len(vals) - 1))], 2),
            "mean": round(sum(vals) / len(vals), 3), "over_1": sum(1 for u in vals if u > 1.0),
            "over_limit": sum(1 for u in vals if u > job.get("limit", 1.0)),
            "hist": {f"{t:.1f}": sum(1 for u in vals if u > t) for t in (0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9)},
            "hot": hot, "crossings": _crossings(segs), "ratsnest_mm": round(length)}


if __name__ == "__main__":
    run(congestion)
