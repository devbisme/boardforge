"""Grid A* router with rip-up: C4's finishing router and the critical-net prerouter.

A two-layer (F.Cu/B.Cu) 0.05 mm grid router with vias, from run 2's astar_fix.py. Zone
fills are ignored (outer pours are refilled afterwards; inner planes are never routed on).
For a pad on a plane net whose partner is unreachable, the goal is any legal via site.

Job (mode "fix"): pcb, drc (kicad-cli DRC JSON), ripup, max_expansions, + rules below.
  Routes each unconnected pair in the DRC report. With ripup, a failed pair may cross
  other (non-plane) tracks at a cost; the crossed tracks are removed and the process stops
  after that one rip-up (SWIG proxies get unreliable after Remove()+Add() churn), so the
  caller loops.
Job (mode "repair"): pcb, drc. Removes the unlocked tracks/vias behind clearance, width
  and short errors (e.g. a router's necked or off-grid copper) so ``fix`` reroutes them.
Job (mode "preroute"): pcb, nets, width, + rules. Connects each net's pads (nearest-first
  spanning tree) and locks the result.
Rules: plane_nets, via_diameter, via_drill, default_rule [w, clr], net_rules {net: [w, clr]}.
"""

import heapq
import json
import math

import pcbnew

from boardsmith.drc import repairable
from boardsmith.pcb.common import run

MM = pcbnew.FromMM
TOMM = pcbnew.ToMM
RES = 0.05
GRID_MARGIN = 0.04  # clearance margin for grid rounding (0.025 left 0.147 for 0.15)
RIP_COST = 1000
HIST_WEIGHT = 2  # each earlier rip-up of a net makes ripping it again this much costlier
MAX_EXP = 400000
W = 0.15
CLR = 0.175
VIA_D, VIA_DRILL = 0.45, 0.2
PLANE_NETS = set()
DEFAULT_RULE = (0.15, 0.15)
RULES = {}
HIST = {}  # {net: times ripped in earlier passes}
LAYERS = (pcbnew.F_Cu, pcbnew.B_Cu)


def configure(job):
    global VIA_D, VIA_DRILL, PLANE_NETS, DEFAULT_RULE, RULES, MAX_EXP, HIST
    VIA_D, VIA_DRILL = job["via_diameter"], job["via_drill"]
    PLANE_NETS = set(job.get("plane_nets", []))
    DEFAULT_RULE = tuple(job["default_rule"])
    RULES = {k: tuple(v) for k, v in job.get("net_rules", {}).items()}
    MAX_EXP = int(job.get("max_expansions", MAX_EXP))
    HIST = dict(job.get("rip_history", {}))


def seg_dist(px, py, ax, ay, bx, by):
    vx, vy = bx - ax, by - ay
    L = vx * vx + vy * vy
    t = 0 if L == 0 else max(0, min(1, ((px - ax) * vx + (py - ay) * vy) / L))
    return math.hypot(px - ax - t * vx, py - ay - t * vy)


class Item:
    def __init__(self, kind, net, layers, geom):
        self.kind, self.net, self.layers, self.geom = kind, net, layers, geom

    def dist(self, x, y):
        k, g = self.kind, self.geom
        if k == "pad":
            poly = g
            # distance to polygon (0 inside)
            inside = False
            n = len(poly)
            d = 1e9
            for i in range(n):
                ax, ay = poly[i]
                bx, by = poly[(i + 1) % n]
                d = min(d, seg_dist(x, y, ax, ay, bx, by))
                if (ay > y) != (by > y) and x < (bx - ax) * (y - ay) / (by - ay + 1e-12) + ax:
                    inside = not inside
            return 0.0 if inside else d
        if k == "track":
            ax, ay, bx, by, w = g
            return max(0.0, seg_dist(x, y, ax, ay, bx, by) - w / 2)
        if k == "via":
            cx, cy, d = g
            return max(0.0, math.hypot(x - cx, y - cy) - d / 2)

    def bbox(self):
        k, g = self.kind, self.geom
        if k == "pad":
            xs = [p[0] for p in g]
            ys = [p[1] for p in g]
            return min(xs), min(ys), max(xs), max(ys)
        if k == "track":
            ax, ay, bx, by, w = g
            return min(ax, bx) - w / 2, min(ay, by) - w / 2, max(ax, bx) + w / 2, max(ay, by) + w / 2
        cx, cy, d = g
        return cx - d / 2, cy - d / 2, cx + d / 2, cy + d / 2


def collect(board):
    items = []
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            if not pad.IsOnCopperLayer():
                continue
            lays = tuple(l for l in LAYERS if pad.IsOnLayer(l))
            if not lays:
                continue
            ps = pad.GetEffectivePolygon(lays[0], pcbnew.ERROR_OUTSIDE)
            ol = ps.Outline(0)
            poly = [(TOMM(ol.CPoint(i).x), TOMM(ol.CPoint(i).y)) for i in range(ol.PointCount())]
            it = Item("pad", pad.GetNetname(), lays, poly)
            it.obj = pad
            items.append(it)
            if pad.GetDrillSize().x > 0:  # holes block both layers
                it.layers = LAYERS
    for t in board.GetTracks():
        if t.GetClass() == "PCB_VIA":
            it = Item("via", t.GetNetname(), LAYERS, (TOMM(t.GetPosition().x), TOMM(t.GetPosition().y),
                                                      TOMM(t.GetWidth(pcbnew.F_Cu))))
        else:
            if t.GetLayer() not in LAYERS:
                continue
            it = Item("track", t.GetNetname(), (t.GetLayer(),),
                      (TOMM(t.GetStart().x), TOMM(t.GetStart().y), TOMM(t.GetEnd().x), TOMM(t.GetEnd().y),
                       TOMM(t.GetWidth())))
        it.obj = t
        items.append(it)
    return items


def find_item(items, desc, pos):
    x, y = pos["x"], pos["y"]
    net = desc[desc.index("[") + 1: desc.index("]")]
    best = None
    for it in items:
        if it.net != net:
            continue
        if desc.startswith("Pad") and it.kind != "pad":
            continue
        if desc.startswith("Track") and it.kind != "track":
            continue
        if desc.startswith("Via") and it.kind != "via":
            continue
        d = it.dist(x, y)
        if best is None or d < best[0]:
            best = (d, it)
    return best[1] if best and best[0] < 0.05 else None


def route(board, items, a, b, net, edge, soft_ok=False, width=None):
    """A* from item a to item b (b may be None -> any via site for plane nets)."""
    global W, CLR
    w, clr = RULES.get(net, DEFAULT_RULE)
    W = width or w
    CLR = clr + GRID_MARGIN
    pts = [a.bbox()] + ([b.bbox()] if b else [])
    for margin in (3.0, 6.0, 10.0, 16.0):
        x0 = min(p[0] for p in pts) - margin
        y0 = min(p[1] for p in pts) - margin
        x1 = max(p[2] for p in pts) + margin
        y1 = max(p[3] for p in pts) + margin
        x0, y0 = max(x0, edge[0] + 0.4), max(y0, edge[1] + 0.4)
        x1, y1 = min(x1, edge[2] - 0.4), min(y1, edge[3] - 0.4)
        nx, ny = int((x1 - x0) / RES) + 1, int((y1 - y0) / RES) + 1
        local = [it for it in items if not (it.bbox()[2] < x0 - 1 or it.bbox()[0] > x1 + 1 or
                                            it.bbox()[3] < y0 - 1 or it.bbox()[1] > y1 + 1)]
        foreign = [it for it in local if it.net != net]
        own = [it for it in local if it.net == net]

        # Obstacle rasters: blocked[(li, rad)] is a bytearray over the window, 1 = blocked.
        rasters = {}
        SOFT = soft_ok

        def rippable(it):
            return SOFT and it.kind in ("track", "via") and it.net not in PLANE_NETS

        def raster(li, rad):
            key = (li, rad)
            if key in rasters:
                return rasters[key]
            grid = bytearray(nx * ny)
            soft = {}
            for it in foreign:
                if LAYERS[li] not in it.layers:
                    continue
                # KiCad applies the larger of the two nets' clearances.
                reach = rad + max(CLR, RULES.get(it.net, DEFAULT_RULE)[1] + GRID_MARGIN)
                rip = rippable(it)
                bb = it.bbox()
                i0 = max(0, int((bb[0] - reach - x0) / RES))
                i1 = min(nx - 1, int((bb[2] + reach - x0) / RES) + 1)
                j0 = max(0, int((bb[1] - reach - y0) / RES))
                j1 = min(ny - 1, int((bb[3] + reach - y0) / RES) + 1)
                for j in range(j0, j1 + 1):
                    y = y0 + j * RES
                    row = j * nx
                    for i in range(i0, i1 + 1):
                        c = row + i
                        if grid[c] == 1:
                            continue
                        if it.dist(x0 + i * RES, y) < reach:
                            if rip:
                                grid[c] = 2
                                soft.setdefault(c, []).append(it)
                            else:
                                grid[c] = 1
                                soft.pop(c, None)
            rasters[key] = (grid, soft)
            return rasters[key]

        def free(i, j, li, rad):
            return not raster(li, rad)[0][j * nx + i]

        def cell_cost(i, j, li, rad):
            """None = hard blocked, else extra cost (soft = crossing a rippable foreign track)."""
            grid, soft = raster(li, rad)
            v = grid[j * nx + i]
            if v != 2:
                return None if v == 1 else 0
            return RIP_COST * (1 + HIST_WEIGHT * max(HIST.get(it.net, 0) for it in soft[j * nx + i]))

        def on(it, i, j, li):
            return LAYERS[li] in it.layers and it.dist(x0 + i * RES, y0 + j * RES) < 1e-6

        def via_ok(i, j):
            return free(i, j, 0, VIA_D / 2) and free(i, j, 1, VIA_D / 2)

        # start cells: grid cells inside item a on its layers
        def cells_of(it):
            bb = it.bbox()
            out = []
            for i in range(max(0, int((bb[0] - x0) / RES)), min(nx, int((bb[2] - x0) / RES) + 2)):
                for j in range(max(0, int((bb[1] - y0) / RES)), min(ny, int((bb[3] - y0) / RES) + 2)):
                    for li in range(2):
                        if on(it, i, j, li):
                            out.append((i, j, li))
            return out

        starts = cells_of(a)
        if b is not None:
            goals = set(cells_of(b))
            gx = sum(c[0] for c in goals) / max(1, len(goals))
            gy = sum(c[1] for c in goals) / max(1, len(goals))
        else:
            goals = None
            gx = gy = None
        if not starts or (b is not None and not goals):
            return None

        def h(i, j):
            return 0 if gx is None else math.hypot(i - gx, j - gy)

        openq = []
        came = {}
        g = {}
        for s in starts:
            g[s] = 0
            heapq.heappush(openq, (h(s[0], s[1]), 0, s))
        steps = [(1, 0, 1), (-1, 0, 1), (0, 1, 1), (0, -1, 1), (1, 1, 1.414), (1, -1, 1.414), (-1, 1, 1.414),
                 (-1, -1, 1.414)]
        found = None
        n_exp = 0
        at_edge = False  # the search reached the window edge (a bigger window might help)
        while openq and n_exp < MAX_EXP:
            f, gc, cur = heapq.heappop(openq)
            if gc > g.get(cur, 1e18):
                continue
            n_exp += 1
            i, j, li = cur
            if goals is not None and cur in goals:
                found = cur
                break
            if goals is None and cur not in starts and via_ok(i, j):
                found = (i, j, li, "via")
                break
            for di, dj, c in steps:
                ni, nj = i + di, j + dj
                if not (0 <= ni < nx and 0 <= nj < ny):
                    at_edge = True
                    continue
                nxt = (ni, nj, li)
                inside_own = (goals is not None and nxt in goals) or any(on(it, ni, nj, li) for it in (a,))
                extra = 0
                if not inside_own:
                    extra = cell_cost(ni, nj, li, W / 2)
                    if extra is None:
                        continue
                ng = gc + c + extra
                if ng < g.get(nxt, 1e18):
                    g[nxt] = ng
                    came[nxt] = cur
                    heapq.heappush(openq, (ng + h(ni, nj), ng, nxt))
            # layer change via
            if goals is not None and via_ok(i, j):
                nxt = (i, j, 1 - li)
                ng = gc + 40
                if ng < g.get(nxt, 1e18):
                    g[nxt] = ng
                    came[nxt] = cur
                    heapq.heappush(openq, (ng + h(i, j), ng, nxt))
        print(f"   search {net} {x1 - x0:.1f}x{y1 - y0:.1f} mm: expanded {n_exp}, found={bool(found)}")
        if not found and not at_edge and n_exp < MAX_EXP:
            return None  # start or goal walled in: a bigger window won't help
        if found:
            end_via = len(found) == 4
            cur = found[:3]
            path = [cur]
            while cur in came:
                cur = came[cur]
                path.append(cur)
            path.reverse()
            # Line-of-sight smoothing per layer run: drop intermediate points when the straight
            # segment between kept points is clear.
            def clear_line(p, q):
                (i1, j1, l1), (i2, j2, _l) = p, q
                n = int(max(abs(i2 - i1), abs(j2 - j1))) + 1
                for k in range(n + 1):
                    i = round(i1 + (i2 - i1) * k / n)
                    j = round(j1 + (j2 - j1) * k / n)
                    if (i, j, l1) in starts_set or (goals is not None and (i, j, l1) in goals):
                        continue
                    if not free(i, j, l1, W / 2):
                        return False
                return True
            starts_set = set(starts)
            out = [path[0]]
            k = 0
            while k < len(path) - 1:
                # furthest reachable point on the same layer
                best = k + 1
                m = k + 2
                while m < len(path) and path[m][2] == path[k][2] and clear_line(path[k], path[m]):
                    best = m
                    m += 1
                out.append(path[best])
                k = best
            crossed = set()
            for i, j, li in path:
                crossed.update(raster(li, W / 2)[1].get(j * nx + i, []))
            for t in range(len(out) - 1):  # smoothed segments may cut through other soft cells
                (i1, j1, l1), (i2, j2, _l) = out[t], out[t + 1]
                n = int(max(abs(i2 - i1), abs(j2 - j1))) + 1
                for k in range(n + 1):
                    i = round(i1 + (i2 - i1) * k / n)
                    j = round(j1 + (j2 - j1) * k / n)
                    crossed.update(raster(l1, W / 2)[1].get(j * nx + i, []))
            for i, j, li in path:
                if li != path[0][2]:
                    pass
            route.crossed = crossed
            return [(x0 + i * RES, y0 + j * RES, li) for i, j, li in out], end_via
    return None


def emit(board, net, path, end_via):
    ni = board.FindNet(net)
    # compress collinear runs
    pts = [path[0]]
    for p in path[1:]:
        pts.append(p)
        if len(pts) >= 3:
            (ax, ay, al), (bx, by, bl), (cx, cy, cl) = pts[-3:]
            if al == bl == cl and abs((bx - ax) * (cy - by) - (by - ay) * (cx - bx)) < 1e-9:
                pts.pop(-2)
    n_t = n_v = 0
    for (ax, ay, al), (bx, by, bl) in zip(pts, pts[1:]):
        if al != bl:
            v = pcbnew.PCB_VIA(board)
            v.SetPosition(pcbnew.VECTOR2I(MM(ax), MM(ay)))
            v.SetWidth(MM(VIA_D))
            v.SetDrill(MM(VIA_DRILL))
            v.SetNet(ni)
            board.Add(v)
            n_v += 1
            continue
        if math.hypot(bx - ax, by - ay) < 1e-6:
            continue
        t = pcbnew.PCB_TRACK(board)
        t.SetStart(pcbnew.VECTOR2I(MM(ax), MM(ay)))
        t.SetEnd(pcbnew.VECTOR2I(MM(bx), MM(by)))
        t.SetWidth(MM(W))
        t.SetLayer(LAYERS[al])
        t.SetNet(ni)
        board.Add(t)
        n_t += 1
    if end_via:
        x, y, _l = pts[-1]
        v = pcbnew.PCB_VIA(board)
        v.SetPosition(pcbnew.VECTOR2I(MM(x), MM(y)))
        v.SetWidth(MM(VIA_D))
        v.SetDrill(MM(VIA_DRILL))
        v.SetNet(ni)
        board.Add(v)
        n_v += 1
    return n_t, n_v


def edge_of(board):
    bb = board.GetBoardEdgesBoundingBox()
    return (TOMM(bb.GetLeft()), TOMM(bb.GetTop()), TOMM(bb.GetRight()), TOMM(bb.GetBottom()))


def fix(job):
    """Route the DRC report's unconnected pairs.

    Pairs whose nets already triggered rip-ups (job "attempts") go last, so one stubborn pair
    can't take every rip-up pass. A pair that fails at its net-class width is retried at the
    default width before ripping anything up.
    """
    configure(job)
    board = pcbnew.LoadBoard(job["pcb"])
    edge = edge_of(board)
    with open(job["drc"]) as f:
        report = json.load(f)
    attempts = job.get("attempts", {})
    pairs = []
    for u in report.get("unconnected_items", []):
        (da, pa), (db, pb) = [(i["description"], i["pos"]) for i in u["items"]]
        if da.startswith("Zone") or db.startswith("Zone"):
            continue
        pairs.append((da, pa, db, pb))
    pairs.sort(key=lambda p: attempts.get(p[0][p[0].index("[") + 1: p[0].index("]")], 0))
    routed, failed, ripped = [], [], []
    for da, pa, db, pb in pairs:
        items = collect(board)
        a = find_item(items, da, pa)
        b = find_item(items, db, pb)
        if a is None or b is None:
            failed.append(f"not found: {da[:50]} / {db[:50]}")
            continue
        net = a.net
        if b.kind == "pad" and a.kind != "pad":  # start from the pad (smaller start set)
            a, b = b, a
        route.crossed = set()
        res = route(board, items, a, b, net, edge)
        if res is None and b.kind == "pad":  # pad to pad: the search from the other end may get through
            res = route(board, items, b, a, net, edge)
        if res is None and RULES.get(net, DEFAULT_RULE)[0] > DEFAULT_RULE[0]:
            res = route(board, items, a, b, net, edge, width=DEFAULT_RULE[0])
        if res is None and net in PLANE_NETS and a.kind == "pad":
            res = route(board, items, a, None, net, edge)
        if res is None and job.get("ripup"):
            route.crossed = set()
            res = route(board, items, a, b, net, edge, soft_ok=True)
            if res is not None and route.crossed:
                nets = sorted({it.net for it in route.crossed})
                for it in route.crossed:
                    board.Remove(it.obj)
                ripped.append({"for": net, "segments": len(route.crossed), "nets": nets})
        if res is None:
            failed.append(f"{net}: {da[:50]} <-> {db[:50]}")
            continue
        path, end_via = res
        emit(board, net, path, end_via)
        routed.append(net)
        if ripped:
            break  # one rip-up per process (see module docstring)
    board.Save(job["pcb"])
    return {"routed": len(routed), "failed": failed, "ripped": ripped}


def repair(job):
    """Remove router copper behind repairable DRC errors; the next DRC lists the gaps as
    unconnected pairs for ``fix``. Of the tracks/vias in a violation, the shortest unlocked
    one not on a plane net goes. Removal only (no Add), so one pass is safe in one process."""
    configure(job)
    board = pcbnew.LoadBoard(job["pcb"])
    with open(job["drc"]) as f:
        report = json.load(f)
    by_uuid = {t.m_Uuid.AsString(): t for t in board.GetTracks()}
    doomed, skipped = {}, []
    for v in repairable(report):
        cands = [by_uuid.get(i.get("uuid")) for i in v["items"]]
        cands = [t for t in cands if t is not None and not t.IsLocked()]
        cands.sort(key=lambda t: (t.GetNetname() in PLANE_NETS, t.GetClass() == "PCB_VIA",
                                  0 if t.GetClass() == "PCB_VIA" else t.GetLength()))
        if not cands:
            skipped.append(f"{v['type']}: {v['description'][:60]}")
            continue
        t = cands[0]
        doomed[t.m_Uuid.AsString()] = t
    for t in doomed.values():
        board.Remove(t)
    board.Save(job["pcb"])
    return {"removed": len(doomed), "skipped": skipped}


def _pads_of(items, net):
    return [it for it in items if it.kind == "pad" and it.net == net]


def preroute(job):
    """Connect each listed net's pads, nearest pair first, and lock the copper."""
    configure(job)
    board = pcbnew.LoadBoard(job["pcb"])
    edge = edge_of(board)
    out, failed = {}, []
    for net in job["nets"]:
        pads = _pads_of(collect(board), net)
        if len(pads) < 2:
            failed.append(f"{net}: {len(pads)} pads")
            continue

        def centre(it):
            x0, y0, x1, y1 = it.bbox()
            return (x0 + x1) / 2, (y0 + y1) / 2

        # Prim's spanning tree over pad centres.
        done, todo, pairs = [pads[0]], pads[1:], []
        while todo:
            best = min(((math.dist(centre(p), centre(q)), i, j) for i, p in enumerate(done)
                        for j, q in enumerate(todo)))
            _, i, j = best
            pairs.append((done[i].obj, todo[j].obj))
            done.append(todo.pop(j))
        nt = nv = 0
        for pa, pb in pairs:
            items = collect(board)
            a = next(it for it in items if it.kind == "pad" and it.obj.m_Uuid.AsString() == pa.m_Uuid.AsString())
            b = next(it for it in items if it.kind == "pad" and it.obj.m_Uuid.AsString() == pb.m_Uuid.AsString())
            res = route(board, items, a, b, net, edge, width=job.get("width"))
            if res is None:
                failed.append(f"{net}: {pa.GetParentFootprint().GetReference()}.{pa.GetNumber()} -> "
                              f"{pb.GetParentFootprint().GetReference()}.{pb.GetNumber()}")
                continue
            t, v = emit(board, net, *res)
            nt, nv = nt + t, nv + v
        for t in board.GetTracks():
            if t.GetNetname() == net:
                t.SetLocked(True)
        out[net] = {"tracks": nt, "vias": nv}
    board.Save(job["pcb"])
    return {"nets": out, "failed": failed, "ok": not failed}


def main(job):
    return {"preroute": preroute, "repair": repair}.get(job.get("mode"), fix)(job)


if __name__ == "__main__":
    run(main)
