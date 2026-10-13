"""C3: placement. Run 2's placer, with its design-specific tables moved into the job.

1. Anchors (from the floorplan in boardsmith.toml) fix the main parts.
2. Other multi-pin parts with no anchor are shelf-packed in sheet order.
3. Passives go next to the pad they serve, repeated so chains (R -> C -> IC) resolve.
   Decaps of ``bottom_decap_owners`` go on B.Cu under the pin row.
4. Leftovers spiral out from the board centre.

Job: pcb, anchors [{ref|value, sheet, at, rot, side}], plane_nets, rails, ground_nets,
bottom_decap_owners (regex), keepout (mm around the edge).
"""

import math
import re

import pcbnew

from boardsmith.pcb.common import MM, TOMM, courtyard_rect, edge_rect, overlap, pt, run


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

    occupied = {"F": [courtyard_rect(fps[r], 0.5) for r in holes], "B": []}
    placed = {}

    def inside(b):
        return (b[0] >= ex0 + keep and b[1] >= ey0 + keep and b[2] <= ex1 - keep
                and b[3] <= ey1 - keep)

    def put(fp, x, y, rot, bottom=False):
        if bottom and not fp.IsFlipped():
            fp.Flip(fp.GetPosition(), pcbnew.FLIP_DIRECTION_LEFT_RIGHT)
        fp.SetOrientationDegrees(rot)
        fp.SetPosition(pt(x, y))

    def try_place(fp, tx, ty, rot, max_r=25.0, step=0.25, bottom=False):
        occ = occupied["B" if bottom else "F"]
        put(fp, tx, ty, rot, bottom)
        r = 0.0
        while r <= max_r:
            n = max(1, int(2 * math.pi * r / step)) if r > 0 else 1
            for k in range(n):
                a = 2 * math.pi * k / n
                x, y = tx + r * math.cos(a), ty + r * math.sin(a)
                fp.SetPosition(pt(x, y))
                b = courtyard_rect(fp, 0.25)
                if inside(b) and not any(overlap(b, o) for o in occ):
                    occ.append(b)
                    return True
            r += step
        return False

    # ---- 1. anchors
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
        put(fp, ex0 + a["at"][0], ey0 + a["at"][1], a.get("rot", 0), bottom)
        placed[hits[0]] = fp
        occupied["B" if bottom else "F"].append(courtyard_rect(fp, 0.25))

    # ---- 2. unanchored multi-pin parts: shelf-pack in sheet order
    big = [r for r in parts if r not in placed and parts[r].GetPadCount() > 4]
    big.sort(key=lambda r: (parts[r].GetSheetname() or "", -parts[r].GetPadCount(), r))
    if big:
        x, y, row_h = ex0 + keep + 6, ey0 + keep + 6, 0.0
        for r in big:
            fp = parts[r]
            put(fp, 0, 0, 0)
            b = courtyard_rect(fp)
            bw, bh = b[2] - b[0], b[3] - b[1]
            if x + bw + 6 > ex1 - keep and x > ex0 + keep + 6:
                x, y, row_h = ex0 + keep + 6, y + row_h + 6, 0.0
            if try_place(fp, x + bw / 2, y + bh / 2, 0, max_r=40):
                placed[r] = fp
            x += bw + 6
            row_h = max(row_h, bh)

    # ---- 3. passives next to the pad they serve
    pad_use = {}

    def owner_pads(net):
        return [(r, p) for r, fp in placed.items() for p in fp.Pads() if p.GetNetname() == net]

    remaining = [r for r in parts if r not in placed]
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
            is_decap = len(my_nets) == 2 and set(my_nets) <= rails
            bottom = bool(is_decap and bottom_owner_rx and re.search(bottom_owner_rx, owner.GetValue()))
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
            if try_place(fp, tx, ty, rot, bottom=bottom):
                placed[ref] = fp
                remaining.remove(ref)
                progress = True
        if not progress:
            break

    # ---- 4. leftovers
    failed = []
    for ref in remaining:
        if try_place(parts[ref], (ex0 + ex1) / 2, (ey0 + ey1) / 2, 0, max_r=max(ex1 - ex0, ey1 - ey0)):
            warnings.append(f"{ref} ({parts[ref].GetValue()}) placed with no anchor or partner")
        else:
            failed.append(ref)

    board.Save(job["pcb"])
    bottom = sum(1 for fp in parts.values() if fp.IsFlipped())
    return {"anchored": len(job.get("anchors", [])) - sum(w.startswith("anchor") for w in warnings),
            "placed": len(parts) - len(failed), "bottom_side": bottom, "unplaced": failed,
            "warnings": warnings, "ok": not failed}


if __name__ == "__main__":
    run(place)
