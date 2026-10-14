"""C3: placement. Run 2's placer, with its design-specific tables moved into the job.

1. Anchors (from the floorplan in boardsmith.toml) fix the main parts.
2. Other multi-pin parts with no anchor are shelf-packed in sheet order, each inside the
   first floorplan region whose sheet/value regexes match it (else anywhere on the board).
3. Passives go next to the pad they serve, repeated so chains (R -> C -> IC) resolve.
   Decaps go first, smallest value first, so the high-frequency ones get the spots nearest
   the pins. Decaps of ``bottom_decap_owners`` go on B.Cu under the pin row; with
   ``decap_bottom``, any other decap between plane nets that can't get within
   ``decap_max_mm`` of its pin on the front goes on B.Cu if that is closer.
4. Leftovers spiral out from the board centre.

Job: pcb, design_size ([w, h] the anchor and region positions are for; on a smaller board
they scale, except an anchor's offset from the edges it names),
anchors [{ref|value, sheet, at, rot, side, edge (left|right|top|bottom or a list)}],
regions [{sheet, value, rect (mm from the top-left corner) | area (fractions of the board)}],
ic_gap (mm between shelf-packed parts), plane_nets, rails, ground_nets,
bottom_decap_owners (regex), decap_bottom (bool), decap_max_mm, keepout (mm around the edge).
"""

import math
import re

import pcbnew

from boardsmith.pcb.common import MM, TOMM, courtyard_rect, edge_rect, farads, overlap, pt, run


def place(job):
    board = pcbnew.LoadBoard(job["pcb"])
    ex0, ey0, ex1, ey1 = edge_rect(board)
    keep = job.get("keepout", 0.5)
    plane_nets = set(job.get("plane_nets", []))
    rails = set(job.get("rails", [])) | plane_nets
    ground = set(job.get("ground_nets", []))
    bottom_owner_rx = job.get("bottom_decap_owners") or None
    warnings = []

    fps = {fp.GetReference(): fp for fp in board.GetFootprints()}
    holes = {r for r in fps if fps[r].IsBoardOnly()}
    parts = {r: fp for r, fp in fps.items() if r not in holes}
    nets_of = {r: [p.GetNetname() for p in fp.Pads() if p.GetNetname()] for r, fp in parts.items()}
    net_size = {}
    for ns in nets_of.values():
        for n in ns:
            net_size[n] = net_size.get(n, 0) + 1

    occupied = {"F": [courtyard_rect(fps[r], 0.5) for r in holes],
                "B": [courtyard_rect(fps[r], 0.5) for r in holes]}
    placed = {}

    def inside_box(b, box):
        return b[0] >= box[0] and b[1] >= box[1] and b[2] <= box[2] and b[3] <= box[3]

    def inside(b):
        return (b[0] >= ex0 + keep and b[1] >= ey0 + keep and b[2] <= ex1 - keep
                and b[3] <= ey1 - keep)

    def put(fp, x, y, rot, bottom=False):
        if bottom != fp.IsFlipped():
            fp.Flip(fp.GetPosition(), pcbnew.FLIP_DIRECTION_LEFT_RIGHT)
        fp.SetOrientationDegrees(rot)
        fp.SetPosition(pt(x, y))

    def pin_rects(fp):
        """Through-hole pads of fp, which take up the other side too (mm, with clearance)."""
        out = []
        for p in fp.Pads():
            if p.GetAttribute() in (pcbnew.PAD_ATTRIB_PTH, pcbnew.PAD_ATTRIB_NPTH):
                bb = p.GetBoundingBox()
                out.append((TOMM(bb.GetLeft()) - 0.5, TOMM(bb.GetTop()) - 0.5,
                            TOMM(bb.GetRight()) + 0.5, TOMM(bb.GetBottom()) + 0.5))
        return out

    def claim(fp, rect, bottom):
        occupied["B" if bottom else "F"].append(rect)
        occupied["F" if bottom else "B"].extend(pin_rects(fp))

    def unclaim(fp, bottom):
        """Undo the last claim(fp, ...)."""
        occupied["B" if bottom else "F"].pop()
        for _ in pin_rects(fp):
            occupied["F" if bottom else "B"].pop()

    def try_place(fp, tx, ty, rot, max_r=25.0, step=0.25, bottom=False, box=None, edge_ok=True,
                  margin=0.25):
        occ = occupied["B" if bottom else "F"]
        put(fp, tx, ty, rot, bottom)
        r = 0.0
        while r <= max_r:
            n = max(1, int(2 * math.pi * r / step)) if r > 0 else 1
            for k in range(n):
                a = 2 * math.pi * k / n
                x, y = tx + r * math.cos(a), ty + r * math.sin(a)
                fp.SetPosition(pt(x, y))
                b = courtyard_rect(fp, margin)
                if (inside(b) or not edge_ok) and (box is None or inside_box(b, box)) \
                        and not any(overlap(b, o) for o in occ):
                    claim(fp, b, bottom)
                    return True
            r += step
        return False

    # Positions in the config are for a board of ``design_size``; on a smaller (compacted)
    # board they scale with it, except offsets from the edges an anchor is tied to.
    dw, dh = job.get("design_size") or (ex1 - ex0, ey1 - ey0)
    sx, sy = (ex1 - ex0) / dw, (ey1 - ey0) / dh

    def anchor_xy(a):
        x, y = a["at"]
        edges = a.get("edge", [])
        edges = [edges] if isinstance(edges, str) else edges
        x = ex0 + x if "left" in edges else ex1 - (dw - x) if "right" in edges else ex0 + x * sx
        y = ey0 + y if "top" in edges else ey1 - (dh - y) if "bottom" in edges else ey0 + y * sy
        return x, y

    # ---- 1. anchors
    anchor_overlaps = []
    for a in job.get("anchors", []):
        if a.get("ref"):
            hits = [a["ref"]] if a["ref"] in parts else []
        else:
            hits = [r for r, fp in parts.items() if re.search(a["value"], fp.GetValue())
                    and re.search(a.get("sheet", ""), fp.GetSheetname() or "")]
        if len(hits) != 1:
            warnings.append(f"anchor {a.get('ref') or a.get('value')!r}/{a.get('sheet', '')!r} "
                            f"matched {hits}")
            continue
        fp = parts[hits[0]]
        bottom = a.get("side", "F") == "B"
        x, y = anchor_xy(a)
        # An anchor that lands on an earlier one (on a compacted board) moves to the nearest
        # free spot, up to anchor_slack mm.
        # (Edge connectors hang over the board edge on purpose: only other parts count.)
        ok = try_place(fp, x, y, a.get("rot", 0), max_r=job.get("anchor_slack", 3.0), bottom=bottom,
                       edge_ok=False, margin=0.0)
        dx, dy = (TOMM(fp.GetPosition().x) - x, TOMM(fp.GetPosition().y) - y) if ok else (0, 0)
        edges = a.get("edge", [])
        edges = [edges] if isinstance(edges, str) else edges
        if ok and ((abs(dx) > 0.01 and {"left", "right"} & set(edges))
                   or (abs(dy) > 0.01 and {"top", "bottom"} & set(edges))):
            unclaim(fp, bottom)  # moved off its edge: not allowed
            ok = False
        if not ok:
            put(fp, x, y, a.get("rot", 0), bottom)
            claim(fp, courtyard_rect(fp, 0.25), bottom)
            anchor_overlaps.append(hits[0])
            warnings.append(f"anchor {hits[0]} ({fp.GetValue()}) overlaps another part")
        elif math.hypot(dx, dy) > 0.01:
            warnings.append(f"anchor {hits[0]} ({fp.GetValue()}) moved {math.hypot(dx, dy):.1f} mm "
                            f"to clear another part")
        placed[hits[0]] = fp

    # ---- 2. unanchored multi-pin parts: shelf-pack in sheet order, inside their region
    def region_of(fp):
        for g in job.get("regions", []):
            if (re.search(g.get("sheet", ""), fp.GetSheetname() or "")
                    and re.search(g.get("value", ""), fp.GetValue())):
                if "rect" in g:
                    x0, y0, x1, y1 = g["rect"]
                    return ex0 + x0 * sx, ey0 + y0 * sy, ex0 + x1 * sx, ey0 + y1 * sy
                fx0, fy0, fx1, fy1 = g["area"]
                w, h = ex1 - ex0, ey1 - ey0
                return ex0 + fx0 * w, ey0 + fy0 * h, ex0 + fx1 * w, ey0 + fy1 * h
        return None

    board_box = (ex0 + keep, ey0 + keep, ex1 - keep, ey1 - keep)
    big = [r for r in parts if r not in placed and parts[r].GetPadCount() > 4]
    big.sort(key=lambda r: (parts[r].GetSheetname() or "", -parts[r].GetPadCount(), r))
    shelves = {}  # box -> [x, y, row height]
    gap = job.get("ic_gap", 6.0)
    for r in big:
        fp = parts[r]
        box = region_of(fp) or board_box
        m = gap if box is board_box else 1.0  # margin inside the box
        x, y, row_h = shelves.setdefault(box, [box[0] + m, box[1] + m, 0.0])
        put(fp, 0, 0, 0)
        b = courtyard_rect(fp)
        bw, bh = b[2] - b[0], b[3] - b[1]
        if x + bw + m > box[2] and x > box[0] + m:
            x, y, row_h = box[0] + m, y + row_h + gap, 0.0
        tx, ty = x + bw / 2, y + bh / 2
        if box is not board_box:
            # inside the region if it fits, else as near it as the board allows
            if try_place(fp, tx, ty, 0, max_r=40, box=box):
                placed[r] = fp
            else:
                warnings.append(f"{r} ({fp.GetValue()}) doesn't fit its region")
        if r not in placed and try_place(fp, tx, ty, 0, max_r=40):
            placed[r] = fp
        shelves[box] = [x + bw + gap, y, max(row_h, bh)]

    def pad_dist(fp, pad):
        """Distance (mm) from fp's pad on pad's net to pad."""
        q = [q for q in fp.Pads() if q.GetNetname() == pad.GetNetname()]
        if not q:
            return math.inf
        (ax, ay), (bx, by) = (q[0].GetPosition(), pad.GetPosition())
        return TOMM(math.hypot(ax - bx, ay - by))

    def closer_on_bottom(fp, pad, owner):
        """fp is placed on the front, too far from pad: move it to B.Cu under the pad if that
        is closer. Returns True if moved."""
        front = pad_dist(fp, pad)
        if front <= decap_max:
            return False
        pos, rot = fp.GetPosition(), fp.GetOrientationDegrees()
        rect = occupied["F"].pop()
        cx, cy = TOMM(owner.GetPosition().x), TOMM(owner.GetPosition().y)
        px, py = TOMM(pad.GetPosition().x), TOMM(pad.GetPosition().y)
        d = math.hypot(px - cx, py - cy) or 1.0
        bot_rot = 90 if abs(px - cx) > abs(py - cy) else 0
        dist = -1.0 if owner.GetPadCount() > 3 else 0.0  # just inside the pin row
        if try_place(fp, px + dist * (px - cx) / d, py + dist * (py - cy) / d, bot_rot,
                     max_r=decap_max, bottom=True):
            if pad_dist(fp, pad) < front:
                return True
            occupied["B"].pop()
        put(fp, 0, 0, rot)
        fp.SetPosition(pos)
        occupied["F"].append(rect)
        return False

    # ---- 3. passives next to the pad they serve
    pad_use = {}

    def owner_pads(net):
        return [(r, p) for r, fp in placed.items() for p in fp.Pads() if p.GetNetname() == net]

    def is_decap(ref):
        """A capacitor between two rails (not a ferrite or fuse in a rail)."""
        return (ref.startswith("C") and farads(parts[ref].GetValue()) is not None
                and parts[ref].GetPadCount() == 2 and len(nets_of[ref]) == 2
                and set(nets_of[ref]) <= rails)

    def place_key(ref):
        f = farads(parts[ref].GetValue()) if is_decap(ref) else None
        return (0, f if f is not None else math.inf) if is_decap(ref) else (1, 0)

    decap_max = job.get("decap_max_mm", 3.0)
    decap_bottom = job.get("decap_bottom", False)
    remaining = sorted((r for r in parts if r not in placed), key=place_key)  # stable: keeps ref order
    for _ in range(6):
        progress = False
        for ref in list(remaining):
            fp = parts[ref]
            my_nets = nets_of[ref]
            sheet = fp.GetSheetname()
            cand = []
            for n in (n for n in my_nets if n not in plane_nets):
                for r2, pad in owner_pads(n):
                    same = placed[r2].GetSheetname() == sheet
                    cand.append(((0 if same else 1, net_size.get(n, 0)), r2, pad))
            if not cand:
                for n in my_nets:
                    if n in ground:
                        continue
                    for r2, pad in owner_pads(n):
                        same = placed[r2].GetSheetname() == sheet
                        cand.append(((0 if same else 1, -placed[r2].GetPadCount(),
                                      pad_use.get((r2, pad.GetNumber()), 0)), r2, pad))
            if not cand:
                continue
            cand.sort(key=lambda t: (t[0], t[1]))
            _, r2, pad = cand[0]
            pad_use[(r2, pad.GetNumber())] = pad_use.get((r2, pad.GetNumber()), 0) + 1
            owner = placed[r2]
            cx, cy = TOMM(owner.GetPosition().x), TOMM(owner.GetPosition().y)
            px, py = TOMM(pad.GetPosition().x), TOMM(pad.GetPosition().y)
            dx, dy = px - cx, py - cy
            d = math.hypot(dx, dy) or 1.0
            decap = is_decap(ref)
            bottom = bool(decap and bottom_owner_rx and re.search(bottom_owner_rx, owner.GetValue()))
            if bottom:
                dist = -1.8  # under the package, just inside the pin row
            elif owner.GetPadCount() <= 3:
                dist = 1.5
            elif owner.GetPadCount() <= 16:
                dist = 2.6  # small fine-pitch ICs: leave an escape ring
            else:
                dist = 3.5  # big ICs: keep a fan-out ring clear for the buses
            tx, ty = px + dist * dx / d, py + dist * dy / d
            if bottom:
                rot = 90 if abs(dx) > abs(dy) else 0
            else:
                rot = 0 if abs(dx) > abs(dy) else 90
            # Both pads on pins of the same IC (e.g. a flying cap): lie parallel to those pins,
            # pad order matching, so the two connections don't cross.
            if fp.GetPadCount() == 2:
                p1, p2 = list(fp.Pads())
                o1 = [q for q in owner.Pads() if q.GetNetname() == p1.GetNetname()
                      and p1.GetNetname() not in plane_nets]
                o2 = [q for q in owner.Pads() if q.GetNetname() == p2.GetNetname()
                      and p2.GetNetname() not in plane_nets]
                if o1 and o2:
                    ax, ay = TOMM(o1[0].GetPosition().x), TOMM(o1[0].GetPosition().y)
                    bx, by = TOMM(o2[0].GetPosition().x), TOMM(o2[0].GetPosition().y)
                    # pad 1 -> pad 2 runs +x at rot 0; at rot 90 it runs -y (KiCad is CCW)
                    if abs(bx - ax) >= abs(by - ay):
                        rot = 0 if bx > ax else 180
                    else:
                        rot = 90 if by < ay else 270
                    mx, my = (ax + bx) / 2, (ay + by) / 2
                    ux, uy = mx - cx, my - cy
                    du = math.hypot(ux, uy) or 1.0
                    tx, ty = mx + 2.2 * ux / du, my + 2.2 * uy / du
            ok = try_place(fp, tx, ty, rot, bottom=bottom)
            # Only plane decaps: their vias come from the fanout; a decap on another rail
            # would need a routed via next to a crowded pin row.
            if ok and decap and decap_bottom and not bottom and set(my_nets) <= plane_nets | ground:
                ok = closer_on_bottom(fp, pad, owner) or ok
            if ok:
                placed[ref] = fp
                remaining.remove(ref)
                progress = True
        if not progress:
            break

    # ---- 4. leftovers: from the centre of their region, else of the board
    failed = []
    for ref in remaining:
        box = region_of(parts[ref]) or (ex0, ey0, ex1, ey1)
        if try_place(parts[ref], (box[0] + box[2]) / 2, (box[1] + box[3]) / 2, 0,
                     max_r=max(ex1 - ex0, ey1 - ey0)):
            warnings.append(f"{ref} ({parts[ref].GetValue()}) placed with no anchor or partner")
        else:
            failed.append(ref)

    board.Save(job["pcb"])
    bottom = sum(1 for fp in parts.values() if fp.IsFlipped())
    return {"anchored": len(job.get("anchors", [])) - sum(w.startswith("anchor") for w in warnings),
            "placed": len(parts) - len(failed), "bottom_side": bottom, "unplaced": failed,
            "anchor_overlaps": anchor_overlaps,
            "leftovers": sum(1 for w in warnings if "placed with no anchor or partner" in w),
            "warnings": warnings, "ok": not failed}


if __name__ == "__main__":
    run(place)
