"""C3: placement. Run 2's placer, with its design-specific tables moved into the job.

1. Anchors (from the floorplan in boardsmith.toml) fix the main parts.
2. Other multi-pin parts, most-connected first, each at the centroid of its connections to
   parts already placed (directly or through a chain of up to 3 2-pin parts) and in the
   rotation that
   shortens them most, inside the first floorplan region whose sheet/value regexes match
   it, else anywhere on the board, keeping ``ic_gap`` between ICs for their passives and
   fanout. Parts connected to nothing placed are shelf-packed in sheet order.
3. Passives go next to the pad they serve, repeated so chains (R -> C -> IC) resolve.
   Decaps go first, smallest value first, so the high-frequency ones get the spots nearest
   the pins. Decaps of ``bottom_decap_owners`` go on B.Cu under the pin row; with
   ``decap_bottom``, any other decap between plane nets that can't get within
   ``decap_max_mm`` of its pin on the front goes on B.Cu if that is closer.
4. Leftovers spiral out from the board centre.

Job: pcb, design_size ([w, h] the anchor and region positions are for; on a smaller board
they scale, except an anchor's offset from the edges it names),
anchors [{ref|value, sheet, at, rot, side, edge (left|right|top|bottom or a list),
facing (left|right|top|bottom: the rotation that points a connector's mating side there)}],
regions [{sheet, value, rect (mm from the top-left corner) | area (fractions of the board)}],
ic_gap (mm between shelf-packed parts), plane_nets, rails, ground_nets,
bottom_decap_owners (regex), decap_bottom (bool), decap_max_mm, keepout (mm around the edge),
stretch_mm (passives moved further than this from their target count as ``stretch.far``),
connector_path_mm (warn when a connector's signal reaches its first IC from further away).
"""

import math
import re

import pcbnew

from boardsmith.pcb.common import MM, TOMM, courtyard_rect, edge_rect, farads, overlap, pt, run

CONNECTOR = re.compile(r"(J|P|CN)\d")


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

    ic_halos = []  # courtyard + ic_gap/2 of placed multi-pin parts (other ICs keep out)
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
                  margin=0.25, halo=0.0):
        """Nearest free spot to (tx, ty). ``halo``: also keep this far (mm) from other
        multi-pin parts' halos, leaving room around each IC for its passives and fanout."""
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
                    hb = courtyard_rect(fp, halo) if halo else None
                    if hb and any(overlap(hb, o) for o in ic_halos):
                        continue
                    claim(fp, b, bottom)
                    if hb:
                        ic_halos.append(hb)
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

    def pad_mm(p):
        return TOMM(p.GetPosition().x), TOMM(p.GetPosition().y)

    def facing_rot(fp, side, bottom):
        """The rotation that points fp's mating side at the ``side`` edge. The mating side is
        where the body overhangs its pads: from the pads' centroid towards the courtyard's
        centre (a BNC's barrel, a USB-C mouth)."""
        want = {"left": (-1, 0), "right": (1, 0), "top": (0, -1), "bottom": (0, 1)}[side]
        best = None
        for rot in (0, 90, 180, 270):
            put(fp, 0, 0, rot, bottom)
            b = courtyard_rect(fp)
            ps = [pad_mm(p) for p in fp.Pads()]
            px, py = sum(x for x, _ in ps) / len(ps), sum(y for _, y in ps) / len(ps)
            dx, dy = (b[0] + b[2]) / 2 - px, (b[1] + b[3]) / 2 - py
            score = dx * want[0] + dy * want[1]
            if best is None or score > best[0]:
                best = (score, rot)
        return best[1]

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
        if a.get("facing") and "rot" not in a:
            a = dict(a, rot=facing_rot(fp, a["facing"], bottom))
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

    # ---- 2. unanchored multi-pin parts: in their region, else shelf-packed
    gap = job.get("ic_gap", 6.0)
    # Packed by connectivity alone, ICs ended up courtyard to courtyard: their decaps were
    # pushed out (17 over 3 mm) and 20 connections didn't route (dual-ADC run 1 replay).
    ic_halos.extend(courtyard_rect(fp, gap / 2) for fp in placed.values() if fp.GetPadCount() > 4)
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

    def signal(n):
        return bool(n) and n not in rails and n not in ground and not n.startswith("unconnected")

    net_parts = {}
    for r_, ns_ in nets_of.items():
        for n_ in ns_:
            net_parts.setdefault(n_, set()).add(r_)

    def owner_pads(net):
        return [(r, p) for r, fp in placed.items() for p in fp.Pads() if p.GetNetname() == net]

    def links(fp, hops=3):
        """[(pad, [placed pads it connects to])] for fp's signal pads (rails and ground pull
        everything towards the regulators, so they don't count). A connection through a chain
        of up to ``hops`` unplaced 2-pin parts counts too, the nearest placed pads only: an
        op-amp's input reaches its BNC only through the series resistor and attenuator."""
        me = fp.GetReference()
        out = []
        for p in fp.Pads():
            n = p.GetNetname()
            if not signal(n):
                continue
            frontier, seen, q = {n}, {n}, []
            for _ in range(hops + 1):
                q = [(*pad_mm(x), r3) for m in frontier for r3, x in owner_pads(m) if r3 != me]
                if q:
                    break
                nxt = set()
                for m in frontier:
                    for r2 in net_parts.get(m, ()):
                        if r2 not in placed and r2 != me and parts[r2].GetPadCount() == 2:
                            nxt |= {k for k in nets_of[r2] if k not in seen and signal(k)}
                seen |= nxt
                frontier = nxt
                if not frontier:
                    break
            if q:
                out.append((p, q))
        return out

    def place_in_region(fp, box):
        """At the centroid of fp's placed connections, clamped into box, in the rotation
        that makes those connections shortest."""
        ls = links(fp)
        # A connector pulls harder, so the first IC on an input or interface sits by its jack
        # (the rest of the chain then reaches back towards it).
        pts = [(x, y, 3.0 if CONNECTOR.match(r_) else 1.0) for _, qs in ls for x, y, r_ in qs]
        if pts:
            wsum = sum(w for _, _, w in pts)
            tx, ty = sum(x * w for x, _, w in pts) / wsum, sum(y * w for _, y, w in pts) / wsum
        else:
            tx, ty = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        best = None
        for rot in (0, 90, 180, 270):
            put(fp, 0, 0, rot)
            b = courtyard_rect(fp)
            hw, hh = (b[2] - b[0]) / 2 + 1.0, (b[3] - b[1]) / 2 + 1.0
            x = min(max(tx, box[0] + hw), box[2] - hw) - (b[0] + b[2]) / 2
            y = min(max(ty, box[1] + hh), box[3] - hh) - (b[1] + b[3]) / 2
            fp.SetPosition(pt(x, y))
            cost = sum(min(math.dist(pad_mm(p), q[:2]) for q in qs) for p, qs in ls)
            if best is None or cost < best[0] - 1e-6:
                best = (cost, rot, x, y)
        _, rot, x, y = best
        return try_place(fp, x, y, rot, max_r=40, box=box, halo=gap / 2)

    big = [r for r in parts if r not in placed and parts[r].GetPadCount() > 4]
    board_box = (ex0 + keep, ey0 + keep, ex1 - keep, ey1 - keep)
    # Multi-pin parts go where their connections pull them (inside their region, if they
    # have one), most-connected first, so a signal chain grows out from its anchored ends.
    # (Shelf-packing them in sheet order left the dual-ADC front ends at the top of the
    # board, 85 mm from their BNCs, and the ADC's series-resistor packs in a far corner.)
    todo = list(big)
    while todo:
        score = {r: len(links(parts[r])) for r in todo}
        r = max(todo, key=lambda r: (score[r] > 0 or region_of(parts[r]) is not None, score[r],
                                     parts[r].GetPadCount(), r))
        if not score[r] and not region_of(parts[r]):
            break  # nothing placed connects to the rest: shelf-pack them below
        todo.remove(r)
        fp = parts[r]
        box = region_of(fp) or board_box
        if place_in_region(fp, box):
            placed[r] = fp
            continue
        if box is not board_box:
            warnings.append(f"{r} ({fp.GetValue()}) doesn't fit its region")
        if try_place(fp, (box[0] + box[2]) / 2, (box[1] + box[3]) / 2, 0, max_r=40):
            placed[r] = fp

    # Parts nothing placed connects to are shelf-packed in sheet order.
    rest = [r for r in todo if r not in placed]
    rest.sort(key=lambda r: (parts[r].GetSheetname() or "", -parts[r].GetPadCount(), r))
    x, y, row_h = board_box[0] + gap, board_box[1] + gap, 0.0
    for r in rest:
        fp = parts[r]
        put(fp, 0, 0, 0)
        b = courtyard_rect(fp)
        bw, bh = b[2] - b[0], b[3] - b[1]
        if x + bw + gap > board_box[2] and x > board_box[0] + gap:
            x, y, row_h = board_box[0] + gap, y + row_h + gap, 0.0
        if try_place(fp, x + bw / 2, y + bh / 2, 0, max_r=40):
            placed[r] = fp
        x, row_h = x + bw + gap, max(row_h, bh)

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
    stretch = {}  # ref -> (mm moved from its target, mm from its pad to the pad it serves,
    #                       owner.pad)

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
    decap_dist = job.get("decap_dist", 2.6)  # mm out from the pin (ICs of 5+ pads)
    remaining = sorted((r for r in parts if r not in placed), key=place_key)  # stable: keeps ref order
    for _ in range(6):
        progress = False
        for ref in list(remaining):
            fp = parts[ref]
            my_nets = nets_of[ref]
            sheet = fp.GetSheetname()
            cand = []
            decap = is_decap(ref)
            if decap:
                # A decap serves an IC's supply pin: the same sheet's ICs first, then the pin
                # with the fewest decaps so far, then the biggest IC. (The general key below
                # ties on every pad of a rail and falls back to ref order, so decaps lined up
                # on other capacitors, "C55" < "U11", and ended up 4-16 mm from their IC.)
                for n in (n for n in my_nets if n not in ground):
                    for r2, pad in owner_pads(n):
                        if placed[r2].GetPadCount() > 4:
                            cand.append(((0 if placed[r2].GetSheetname() == sheet else 1,
                                          pad_use.get((r2, pad.GetNumber()), 0),
                                          -placed[r2].GetPadCount()), r2, pad))
            if not cand:
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
            bottom = bool(decap and bottom_owner_rx and re.search(bottom_owner_rx, owner.GetValue()))
            if bottom:
                dist = -1.8  # under the package, just inside the pin row
            elif owner.GetPadCount() <= 3:
                dist = 1.5
            elif decap:
                dist = decap_dist
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
            flying = False
            if fp.GetPadCount() == 2:
                p1, p2 = list(fp.Pads())
                o1 = [q for q in owner.Pads() if q.GetNetname() == p1.GetNetname()
                      and p1.GetNetname() not in plane_nets]
                o2 = [q for q in owner.Pads() if q.GetNetname() == p2.GetNetname()
                      and p2.GetNetname() not in plane_nets]
                if o1 and o2:
                    flying = True
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
            if decap and not flying:
                # radial, with the rail pad (not the ground pad) towards the pin
                def reach(r_):
                    put(fp, tx, ty, r_, bottom)
                    return pad_dist(fp, pad)
                rot = min((rot, (rot + 180) % 360), key=reach)
            ok = try_place(fp, tx, ty, rot, bottom=bottom)
            if ok:
                q = fp.GetPosition()
                moved = math.hypot(TOMM(q.x) - tx, TOMM(q.y) - ty)
            # Only plane decaps: their vias come from the fanout; a decap on another rail
            # would need a routed via next to a crowded pin row.
            if ok and decap and decap_bottom and not bottom and set(my_nets) <= plane_nets | ground:
                if closer_on_bottom(fp, pad, owner):
                    moved = 0.0  # under the pin row, where it was meant to go
            if ok:
                stretch[ref] = (round(moved, 2), round(pad_dist(fp, pad), 2), f"{r2}.{pad.GetNumber()}")
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

    paths = _connector_paths(parts, nets_of, net_parts, signal)
    far_mm = job.get("connector_path_mm", 25.0)
    for c in paths:
        if c["mm"] > far_mm:
            warnings.append(f"{c['conn']}.{c['pad']} ({c['net']}) reaches {c['to']} {c['mm']} mm away"
                            + (f" through {', '.join(c['via'])}" if c["via"] else "")
                            + ": anchor that part (or its connector) closer")
    board.Save(job["pcb"])
    bottom = sum(1 for fp in parts.values() if fp.IsFlipped())
    return {"anchored": len(job.get("anchors", [])) - sum(w.startswith("anchor") for w in warnings),
            "placed": len(parts) - len(failed), "bottom_side": bottom, "unplaced": failed,
            "anchor_overlaps": anchor_overlaps,
            "leftovers": sum(1 for w in warnings if "placed with no anchor or partner" in w),
            "stretch": _stretch(stretch, job.get("stretch_mm", 8.0)),
            "stretch_parts": stretch,  # ref -> [moved, reach, owner.pad] (work/ result only)
            "connector_paths": sorted(paths, key=lambda c: -c["mm"])[:12],
            "warnings": warnings, "ok": not failed}


def _connector_paths(parts, nets_of, net_parts, signal, hops=3):
    """For each connector signal pad: the first IC (more than 3 pads) its net reaches,
    directly or through up to ``hops`` 2-pin parts, and how far away it is (mm, pad to pad).
    A long one is an unbuffered input or an interface line crossing the board."""
    def mm(p):
        return TOMM(p.GetPosition().x), TOMM(p.GetPosition().y)

    out = []
    for ref, fp in parts.items():
        if not CONNECTOR.match(ref):
            continue
        for pad in fp.Pads():
            n = pad.GetNetname()
            if not signal(n):
                continue
            frontier, seen, via, hit = {n}, {n}, [], None
            for _ in range(hops + 1):
                ics = [(r2, q) for m in frontier for r2 in sorted(net_parts.get(m, ()))
                       if r2 != ref and parts[r2].GetPadCount() > 3
                       for q in parts[r2].Pads() if q.GetNetname() == m]
                if ics:
                    hit = min(ics, key=lambda t: math.dist(mm(pad), mm(t[1])))
                    break
                nxt = set()
                for m in frontier:
                    for r2 in sorted(net_parts.get(m, ())):
                        if r2 != ref and parts[r2].GetPadCount() == 2:
                            new = {k for k in nets_of[r2] if k not in seen and signal(k)}
                            if new:
                                via.append(r2)
                            nxt |= new
                seen |= nxt
                frontier = nxt
                if not frontier:
                    break
            if hit:
                out.append({"conn": ref, "pad": pad.GetNumber(), "net": n, "to": hit[0],
                            "mm": round(math.dist(mm(pad), mm(hit[1])), 1), "via": via[:4]})
    return out


def _stretch(st, far_mm):
    """How far passives ended up from where they were meant to go: shrinking a board pushes
    them off their pins before any routing channel fills (§4.9)."""
    if not st:
        return {"n": 0, "far": 0}
    mv = sorted(v[0] for v in st.values())
    rc = sorted(v[1] for v in st.values())
    worst = sorted(st.items(), key=lambda kv: -kv[1][0])[:8]
    return {"n": len(st), "far": sum(1 for m in mv if m > far_mm),
            "moved_max": mv[-1], "moved_p90": mv[int(0.9 * (len(mv) - 1))],
            "moved_mean": round(sum(mv) / len(mv), 2),
            "moved_over": {str(t): sum(1 for m in mv if m > t) for t in (1, 2, 3, 5, 8)},
            "reach_max": rc[-1], "reach_p90": rc[int(0.9 * (len(rc) - 1))],
            "reach_over": {str(t): sum(1 for r in rc if r > t) for t in (3, 5, 8, 12)},
            "worst": [[r, m, d, o] for r, (m, d, o) in worst]}


if __name__ == "__main__":
    run(place)
