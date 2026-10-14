"""C5 geometry checks that the analyzers miss, plus board statistics.

- Decap distance: for each 2-pad capacitor between a rail and ground, the distance from its
  rail pad to the nearest IC (> 4 pads) pad on that rail. Bulk caps (>= ``bulk_uf``) are
  reported separately, since they are often placed farther away on purpose.
- Length of each listed net's copper (e.g. clocks).
- Board size, track length, via count, parts per side.

Job: pcb, rails, ground_nets, decap_max_mm, bulk_uf, length_nets.
"""

import math

import pcbnew

from boardsmith.pcb.common import TOMM, edge_rect, farads, run, xy


def main(job):
    b = pcbnew.LoadBoard(job["pcb"])
    rails = set(job.get("rails", []))
    ground = set(job.get("ground_nets", []))
    limit = job.get("decap_max_mm", 3.0)
    bulk = job.get("bulk_uf", 4.7) * 1e-6
    fps = list(b.GetFootprints())
    ics = [fp for fp in fps if fp.GetPadCount() > 4 and not fp.IsBoardOnly()]
    far, far_bulk, checked = [], [], 0
    for fp in fps:
        if not fp.GetReference().startswith("C") or fp.GetPadCount() != 2:
            continue
        pads = list(fp.Pads())
        nets = [p.GetNetname() for p in pads]
        if not (set(nets) & ground) or not (set(nets) & (rails - ground)):
            continue
        rail_pad = next(p for p in pads if p.GetNetname() in rails - ground)
        rx, ry = xy(rail_pad.GetPosition())
        best = None
        for ic in ics:
            for p in ic.Pads():
                if p.GetNetname() == rail_pad.GetNetname():
                    d = math.dist((rx, ry), xy(p.GetPosition()))
                    if best is None or d < best[0]:
                        best = (d, f"{ic.GetReference()}.{p.GetNumber()}")
        if best is None:
            continue
        checked += 1
        if best[0] > limit:
            f = farads(fp.GetValue())
            row = {"ref": fp.GetReference(), "value": fp.GetValue(), "net": rail_pad.GetNetname(),
                   "nearest": best[1], "mm": round(best[0], 1)}
            (far_bulk if f is not None and f >= bulk else far).append(row)

    lengths = {}
    for net in job.get("length_nets", []):
        L = sum(TOMM(t.GetLength()) for t in b.GetTracks()
                if t.GetClass() == "PCB_TRACK" and t.GetNetname() == net)
        lengths[net] = round(L, 1)

    x0, y0, x1, y1 = edge_rect(b)
    tracks = [t for t in b.GetTracks() if t.GetClass() == "PCB_TRACK"]
    return {
        "board_mm": [round(x1 - x0, 1), round(y1 - y0, 1)],
        "copper_layers": b.GetCopperLayerCount(),
        "parts_top": sum(1 for fp in fps if not fp.IsFlipped() and not fp.IsBoardOnly()),
        "parts_bottom": sum(1 for fp in fps if fp.IsFlipped() and not fp.IsBoardOnly()),
        "track_mm": round(sum(TOMM(t.GetLength()) for t in tracks)),
        "vias": sum(1 for t in b.GetTracks() if t.GetClass() == "PCB_VIA"),
        "decaps_checked": checked,
        "decap_far": sorted(far, key=lambda r: -r["mm"]),
        "decap_far_bulk": sorted(far_bulk, key=lambda r: -r["mm"]),
        "net_lengths_mm": lengths,
    }


if __name__ == "__main__":
    run(main)
