"""C4 polish. Phase "cleanup": remove dangling copper. Phase "polish" (default): widen
necked tracks, add outer pours, unlock, size reference text, fill zones, stitch the pours.

Job: pcb, pours [net], min_track, unlock (bool), ref_text {small, large}, plane_nets,
stitch_pitch (mm, 0 = off), via_diameter, via_drill, clearance, edge_clearance.
Run cleanup first, in its own process, then polish.
"""

import pcbnew

from boardforge.pcb.common import MM, add_zone, edge_rect, run

TOL = pcbnew.FromMM(0.01)


def _d(a, b):
    try:
        return ((a.x - b.x) ** 2 + (a.y - b.y) ** 2) ** 0.5
    except AttributeError:  # untyped SWIG proxy (KiCad 10 quirk): treat as connected
        return 0


def _seg_pt(p, a, b):
    """Distance from point p to segment ab."""
    vx, vy = b.x - a.x, b.y - a.y
    L = vx * vx + vy * vy
    t = 0 if L == 0 else max(0.0, min(1.0, ((p.x - a.x) * vx + (p.y - a.y) * vy) / L))
    return ((p.x - a.x - t * vx) ** 2 + (p.y - a.y - t * vy) ** 2) ** 0.5


def _seg_seg(a, b, c, d):
    """Distance between segments ab and cd (enough for touching tests: no crossing case
    needed, as crossing same-layer copper touches at an end distance of 0 or overlaps)."""
    return min(_seg_pt(a, c, d), _seg_pt(b, c, d), _seg_pt(c, a, b), _seg_pt(d, a, b))


def _contacts(t, tracks, net):
    """Same-net tracks and vias whose copper touches track t anywhere along it."""
    hw = t.GetWidth() // 2
    a, b = t.GetStart(), t.GetEnd()
    n = 0
    for u in tracks:
        if u is t or u.GetNetname() != net:
            continue
        if u.GetClass() == "PCB_VIA":
            n += _seg_pt(u.GetPosition(), a, b) < u.GetWidth(pcbnew.F_Cu) // 2 + hw
        elif u.GetLayer() == t.GetLayer():
            n += _seg_seg(a, b, u.GetStart(), u.GetEnd()) < u.GetWidth() // 2 + hw
    return n


def remove_dangling(b, plane_nets):
    """Leaf copper (rip-up leftovers), until stable: tracks with a free end that touch at most
    one other item, and vias with <= 1 connection.
    Plane nets are skipped: their vias connect to the planes. Locked copper is kept.
    Iterates on a snapshot and removes at the end: KiCad 10's track list is unreliable after
    Remove() (a removed via can still show up and keep its tracks "connected")."""
    alive = [t for t in b.GetTracks() if t.GetNetname() not in plane_nets]
    pads = [p for fp in b.GetFootprints() for p in fp.Pads()]
    gone = []
    while True:
        tracks = alive
        removed = set()
        for k, t in enumerate(tracks):
            if t.IsLocked():  # connects others, never removed
                continue
            net = t.GetNetname()
            if t.GetClass() == "PCB_VIA":
                pos = t.GetPosition()
                r = t.GetWidth(pcbnew.F_Cu) // 2  # any same-net copper overlapping the via connects
                n = sum(1 for u in tracks if u is not t and u.GetNetname() == net and (
                    _d(u.GetPosition(), pos) < r + u.GetWidth(pcbnew.F_Cu) // 2 if u.GetClass() == "PCB_VIA"
                    else _seg_pt(pos, u.GetStart(), u.GetEnd()) < r + u.GetWidth() // 2))
                if any(p.GetNetname() == net and p.HitTest(pos, r) for p in pads):
                    n += 1
                if n <= 1:
                    removed.add(k)
                continue
            # An end connects where its copper overlaps other copper, as in KiCad's connectivity:
            # A* ends can sit on the edge of a pad or partway along another track.
            hw = t.GetWidth() // 2
            for end in (t.GetStart(), t.GetEnd()):
                ok = False
                for u in tracks:
                    if u is t or u.GetNetname() != net:
                        continue
                    if u.GetClass() == "PCB_VIA":
                        if _d(u.GetPosition(), end) < u.GetWidth(pcbnew.F_Cu) // 2 + hw:
                            ok = True
                            break
                    elif u.GetLayer() == t.GetLayer() and (
                            _d(u.GetStart(), end) < TOL or _d(u.GetEnd(), end) < TOL or u.HitTest(end, hw)):
                        ok = True
                        break
                if not ok:
                    ok = any(p.GetNetname() == net and p.IsOnLayer(t.GetLayer())
                             and p.HitTest(end, hw) for p in pads)
                if not ok:
                    # A free end, but the track may still carry the route: another track can
                    # join it partway along (A* does), leaving a short stub past the joint.
                    if _contacts(t, tracks, net) <= 1:
                        removed.add(k)
                    break
        if not removed:
            break
        gone += [t for k, t in enumerate(tracks) if k in removed]
        alive = [t for k, t in enumerate(tracks) if k not in removed]
    for t in gone:
        b.Remove(t)
    return len(gone)


def finish(job):
    board = pcbnew.LoadBoard(job["pcb"])
    if job.get("phase") == "cleanup":
        # Its own process: after Remove(), KiCad 10's SWIG track lists go stale.
        n = remove_dangling(board, set(job.get("plane_nets", [])))
        board.Save(job["pcb"])
        return {"dangling_removed": n}

    res = {}
    widened = 0
    min_w = MM(job["min_track"])
    for t in board.GetTracks():
        if t.GetClass() == "PCB_TRACK" and t.GetWidth() < min_w:
            t.SetWidth(min_w)
            widened += 1
    res["widened"] = widened

    x0, y0, x1, y1 = edge_rect(board)
    have = {(z.GetLayer(), z.GetNetname()) for z in board.Zones()}
    pours = []
    for net in job.get("pours", []):
        ni = board.FindNet(net)
        if ni is None:
            continue
        for layer in (pcbnew.F_Cu, pcbnew.B_Cu):
            if (layer, net) not in have:
                add_zone(board, layer, ni, (x0 + 0.3, y0 + 0.3, x1 - 0.3, y1 - 0.3), clearance=0.25)
                pours.append(f"{net}@{board.GetLayerName(layer)}")
    res["pours_added"] = pours

    if job.get("unlock", True):
        for t in board.GetTracks():
            t.SetLocked(False)

    # Silkscreen: references of small passives go to the fab layer (too dense to read);
    # IC and connector references stay on silkscreen.
    small, large = job.get("ref_text", {}).get("small", 0.4), job.get("ref_text", {}).get("large", 0.8)
    for fp in board.GetFootprints():
        ref = fp.Reference()
        if fp.GetPadCount() <= 3 and fp.GetReference()[:1] in "RCLDF" and not fp.IsBoardOnly():
            ref.SetLayer(pcbnew.B_Fab if fp.IsFlipped() else pcbnew.F_Fab)
            ref.SetTextSize(pcbnew.VECTOR2I(MM(small), MM(small)))
            ref.SetTextThickness(MM(small * 0.15))
        else:
            ref.SetTextSize(pcbnew.VECTOR2I(MM(large), MM(large)))
            ref.SetTextThickness(MM(large * 0.15))

    filler = pcbnew.ZONE_FILLER(board)
    filler.Fill(board.Zones())
    if job.get("stitch_pitch") and job.get("pours"):
        res["stitching_vias"] = stitch(board, job)
        filler.Fill(board.Zones())
    board.Save(job["pcb"])
    return res


def stitch(board, job):
    """Stitching vias on a grid wherever every outer pour of a net is filled on both sides,
    so pour islands (e.g. around a through-hole pad) tie to the other side and the planes."""
    pitch, d, drill = job["stitch_pitch"], job["via_diameter"], job["via_drill"]
    clr = job.get("clearance", 0.2)
    x0, y0, x1, y1 = edge_rect(board)
    m = d / 2 + job.get("edge_clearance", 0.3) + 0.1
    x0, y0, x1, y1 = x0 + m, y0 + m, x1 - m, y1 - m
    items = []
    for fp in board.GetFootprints():
        for p in fp.Pads():
            items.append((p.GetNetname(), p.GetBoundingBox()))
    for t in board.GetTracks():
        items.append((t.GetNetname(), t.GetBoundingBox()))
    n = 0
    for net in job["pours"]:
        zones = [z for z in board.Zones() if z.GetNetname() == net
                 and z.GetLayer() in (pcbnew.F_Cu, pcbnew.B_Cu)]
        if len({z.GetLayer() for z in zones}) < 2:
            continue
        ni = board.FindNet(net)
        reach = MM(d / 2 + clr + 0.05)
        y = y0 + pitch / 2
        while y <= y1:
            x = x0 + pitch / 2
            while x <= x1:
                pos = pcbnew.VECTOR2I(MM(x), MM(y))
                if all(z.HitTestFilledArea(z.GetLayer(), pos, MM(d / 2 + 0.1)) for z in zones):
                    box = pcbnew.BOX2I(pcbnew.VECTOR2I(pos.x - reach, pos.y - reach),
                                       pcbnew.VECTOR2I(2 * reach, 2 * reach))
                    if not any(nn != net and bb.Intersects(box) for nn, bb in items) and \
                            not any(nn == net and bb.Contains(pos) for nn, bb in items):
                        v = pcbnew.PCB_VIA(board)
                        v.SetPosition(pos)
                        v.SetWidth(MM(d))
                        v.SetDrill(MM(drill))
                        v.SetNet(ni)
                        board.Add(v)
                        items.append((net, v.GetBoundingBox()))
                        n += 1
                x += pitch
            y += pitch
    return n


if __name__ == "__main__":
    run(finish)
