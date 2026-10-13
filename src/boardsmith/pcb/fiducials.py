"""C6: three board-only fiducials per assembled side, each at the free spot nearest a corner.

Job: pcb, fp_paths, footprint ("Fiducial:Fiducial_1mm_Mask2mm"), sides (["F"] or ["F","B"]),
label (silkscreen board name/revision). Sides with no parts are skipped.
"""

import math

import pcbnew

from boardsmith.pcb.common import MM, edge_rect, load_footprint, rect, run


def main(job):
    b = pcbnew.LoadBoard(job["pcb"])
    for fp in list(b.GetFootprints()):
        if fp.GetReference().startswith("FID"):
            b.Remove(fp)
    x0, y0, x1, y1 = edge_rect(b)
    parts = [fp for fp in b.GetFootprints() if not fp.IsBoardOnly()]
    sides = [s for s in job.get("sides", ["F", "B"])
             if any(fp.IsFlipped() == (s == "B") for fp in parts)]
    placed, failed, n = [], [], 0
    for side in sides:
        layer = pcbnew.B_Cu if side == "B" else pcbnew.F_Cu
        obs = []
        for fp in b.GetFootprints():
            obs += [rect(p.GetBoundingBox()) for p in fp.Pads() if p.IsOnLayer(layer)]
            # GetCourtyard(B_CrtYd) segfaults in KiCad 10 Python; use the bbox per side.
            if fp.IsFlipped() == (side == "B"):
                obs.append(rect(fp.GetBoundingBox(False)))
        obs += [rect(t.GetBoundingBox()) for t in b.GetTracks() if t.IsOnLayer(layer)]

        def free(x, y, r=1.6):
            if x - r < x0 + 1 or x + r > x1 - 1 or y - r < y0 + 1 or y + r > y1 - 1:
                return False
            return all(x + r < a or x - r > c or y + r < d0 or y - r > d1 for a, d0, c, d1 in obs)

        for cx, cy in ((x0 + 9, y0 + 4), (x1 - 9, y0 + 4), (x0 + 9, y1 - 4)):
            spot = None
            for rad in [k * 0.25 for k in range(0, 120)]:
                for a in range(0, 360, 15):
                    x, y = cx + rad * math.cos(math.radians(a)), cy + rad * math.sin(math.radians(a))
                    if free(x, y):
                        spot = (x, y)
                        break
                if spot:
                    break
            n += 1
            if not spot:
                failed.append(f"FID{n} ({side})")
                continue
            fp = load_footprint(job["footprint"], job["fp_paths"])
            fp.SetReference(f"FID{n}")
            fp.SetValue("Fiducial")
            fp.SetBoardOnly(True)
            fp.SetPosition(pcbnew.VECTOR2I(MM(spot[0]), MM(spot[1])))
            b.Add(fp)  # must be on the board before Flip() (segfaults otherwise)
            if side == "B":
                fp.Flip(fp.GetPosition(), pcbnew.FLIP_DIRECTION_LEFT_RIGHT)
            obs.append((spot[0] - 1.6, spot[1] - 1.6, spot[0] + 1.6, spot[1] + 1.6))
            placed.append(f"FID{n} {side} {spot[0]:.2f},{spot[1]:.2f}")
    label = add_label(b, job.get("label", ""), x0, y0, x1, y1)
    pcbnew.ZONE_FILLER(b).Fill(b.Zones())
    b.Save(job["pcb"])
    return {"placed": placed, "failed": failed, "label": label, "ok": not failed}


def add_label(b, text, x0, y0, x1, y1, size=1.0):
    """Board name/revision on F.SilkS at the free spot nearest the bottom-centre."""
    if not text:
        return None
    w, h = 0.75 * size * len(text) + 1.0, size + 1.0
    obs = []
    for fp in b.GetFootprints():
        if not fp.IsFlipped():
            obs.append(rect(fp.GetBoundingBox(False)))
    obs += [rect(t.GetBoundingBox()) for t in b.GetTracks() if t.IsOnLayer(pcbnew.F_Cu)
            and t.GetClass() == "PCB_VIA"]

    def free(x, y):
        if x - w / 2 < x0 + 1 or x + w / 2 > x1 - 1 or y - h / 2 < y0 + 1 or y + h / 2 > y1 - 1:
            return False
        return all(x + w / 2 < a or x - w / 2 > c or y + h / 2 < d0 or y - h / 2 > d1
                   for a, d0, c, d1 in obs)

    cx, cy = (x0 + x1) / 2, y1 - 2.5
    for rad in [k * 0.5 for k in range(0, 200)]:
        for a in range(0, 360, 10):
            x, y = cx + rad * math.cos(math.radians(a)), cy + rad * math.sin(math.radians(a))
            if free(x, y):
                t = pcbnew.PCB_TEXT(b)
                t.SetText(text)
                t.SetLayer(pcbnew.F_SilkS)
                t.SetPosition(pcbnew.VECTOR2I(MM(x), MM(y)))
                t.SetTextSize(pcbnew.VECTOR2I(MM(size), MM(size)))
                t.SetTextThickness(MM(0.15))
                b.Add(t)
                return f"{x:.1f},{y:.1f}"
    return None


if __name__ == "__main__":
    run(main)
