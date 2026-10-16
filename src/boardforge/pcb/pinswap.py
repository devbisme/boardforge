"""C3 pin swap: reassign the signals of a part's swappable pins (FPGA/MCU I/O) to match the
placement, so buses leave both packages in the same order and don't cross.

For each group, the signals that go to the partner are ordered and then matched to the
group's pins by an order-preserving assignment (shortest total distance).
- If every partner pad faces the part or sideways, traces leave each pad outwards and turn
  towards the part, so the order that doesn't cross is the order around the partner's outline
  (clockwise, starting after the widest gap between them), with the pins ordered around the
  part's own outline the other way (facing outlines run in opposite directions). Ordering
  such pads by angle seen from the part's centre (kicad-happy run 2) reverses the ones on the
  partner's side edges; on the dual-ADC board that left an ADC data line unrouted.
- If some partner pads face away (the far row of a TSOP), their traces drop through vias and
  run under the package, and the order seen from the part's centre (run 2) routes better;
  the outline order sends them around the package's ends (dual-ADC runs 12-13).
Group signals that don't reach the partner take the free pins nearest their other pads.

Job: pcb, part (value regex) or ref, groups [{pins: [pad numbers], partner: value regex}].
Result: map {kicad net: pad number}, changed (nets whose pin moved).
"""

import math
import re

import pcbnew

from boardforge.pcb.common import run, xy


def _find(fps, ref=None, value=None):
    if ref:
        hits = [f for f in fps if f.GetReference() == ref]
    else:
        hits = [f for f in fps if re.search(value, f.GetValue())]
    if len(hits) != 1:
        raise ValueError(f"pin swap: {ref or value!r} matched {[f.GetReference() for f in hits]}")
    return hits[0]


def _around(items, centre):
    """items [(xy, key)] in clockwise order (KiCad's y points down) about ``centre``,
    starting after the widest angular gap."""
    cx, cy = centre
    a = sorted((math.atan2(p[1] - cy, p[0] - cx), k) for p, k in items)
    if len(a) < 2:
        return [k for _, k in a]
    gaps = [(a[(i + 1) % len(a)][0] - a[i][0]) % (2 * math.pi) for i in range(len(a))]
    i = max(range(len(a)), key=lambda i: gaps[i])
    return [k for _, k in a[i + 1:] + a[:i + 1]]


def _faces_away(p, partner, centre):
    """Whether the partner pad at p sits on the side of the partner facing away from centre.
    The side is the dominant axis of p's offset, scaled by the pads' extent, so the pins at
    the ends of a long package count as being on their row."""
    px, py = xy(partner.GetPosition())
    xs = [xy(q.GetPosition())[0] - px for q in partner.Pads()]
    ys = [xy(q.GetPosition())[1] - py for q in partner.Pads()]
    hw, hh = max(map(abs, xs)) or 1.0, max(map(abs, ys)) or 1.0
    dx, dy = (p[0] - px) / hw, (p[1] - py) / hh
    nx, ny = (math.copysign(1, dx), 0) if abs(dx) >= abs(dy) else (0, math.copysign(1, dy))
    tx, ty = centre[0] - px, centre[1] - py
    d = math.hypot(tx, ty) or 1.0
    return (nx * tx + ny * ty) / d < -0.5


def _ordered_assign(sig_pos, pin_pos):
    """Order-preserving assignment of signals to pins (len(sig) <= len(pins)) minimising the
    total distance. Returns the chosen pin index per signal."""
    n, m = len(sig_pos), len(pin_pos)
    inf = float("inf")
    # best[i][j]: signals[:i] placed on pins[:j]
    best = [[inf] * (m + 1) for _ in range(n + 1)]
    take = [[False] * (m + 1) for _ in range(n + 1)]
    for j in range(m + 1):
        best[0][j] = 0.0
    for i in range(1, n + 1):
        for j in range(i, m + 1):
            skip = best[i][j - 1]
            use = best[i - 1][j - 1] + math.dist(sig_pos[i - 1], pin_pos[j - 1])
            best[i][j], take[i][j] = (use, True) if use <= skip else (skip, False)
    out, i, j = [], n, m
    while i > 0:
        if take[i][j]:
            out.append(j - 1)
            i -= 1
        j -= 1
    return out[::-1]


def pinswap(job):
    board = pcbnew.LoadBoard(job["pcb"])
    fps = list(board.GetFootprints())
    part = _find(fps, job.get("ref"), job.get("part"))
    centre = xy(part.GetPosition())
    pads = {p.GetNumber(): p for p in part.Pads()}
    result, changed, warnings = {}, [], []
    for g in job["groups"]:
        pins = [str(n) for n in g["pins"] if str(n) in pads]
        missing = [n for n in g["pins"] if str(n) not in pads]
        if missing:
            warnings.append(f"pin swap: {part.GetReference()} has no pads {missing}")
        old = {pads[n].GetNetname(): n for n in pins
               if pads[n].GetNetname() and not pads[n].GetNetname().startswith("unconnected")}
        partner = _find(fps, value=g["partner"]) if g.get("partner") else None
        to_partner, others = [], []
        for net in sorted(old):
            pp = [p for p in partner.Pads() if p.GetNetname() == net] if partner else []
            if pp:
                to_partner.append((xy(min(pp, key=lambda p: p.GetNumber()).GetPosition()), net))
            else:
                ends = [xy(p.GetPosition()) for f in fps if f is not part for p in f.Pads()
                        if p.GetNetname() == net]
                others.append((net, ends))
        sig_xy = dict((k, p) for p, k in to_partner)
        pin_order = _around([(xy(pads[n].GetPosition()), n) for n in pins], centre)[::-1]
        if any(_faces_away(p, partner, centre) for p, _ in to_partner):
            # Some traces run under the partner: keep the order seen from the part (run 2).
            sig_order = _around(to_partner, centre)
            pin_order = pin_order[::-1]
        else:
            sig_order = _around(to_partner, xy(partner.GetPosition())) if partner else []
        chosen = _ordered_assign([sig_xy[s] for s in sig_order],
                                 [xy(pads[n].GetPosition()) for n in pin_order])
        assign = {s: pin_order[j] for s, j in zip(sig_order, chosen)}
        free = [n for n in pin_order if n not in assign.values()]
        for net, ends in others:
            if not free:
                break
            if ends:
                cx = sum(e[0] for e in ends) / len(ends)
                cy = sum(e[1] for e in ends) / len(ends)
                n = min(free, key=lambda n: math.dist(xy(pads[n].GetPosition()), (cx, cy)))
            else:
                n = old[net] if old[net] in free else free[0]
            assign[net] = n
            free.remove(n)
        result.update(assign)
        changed += sorted(net for net, n in assign.items() if old[net] != n)
    return {"map": result, "changed": changed, "part": part.GetReference(), "warnings": warnings}


if __name__ == "__main__":
    run(pinswap)
