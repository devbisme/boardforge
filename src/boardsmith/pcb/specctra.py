"""Specctra DSN export / SES import with KiCad's own converters.

Job: mode ("export" | "import"), pcb, dsn or ses, out (import: routed board path).
"""

import pcbnew

from boardsmith.pcb.common import run


def main(job):
    board = pcbnew.LoadBoard(job["pcb"])
    if job["mode"] == "export":
        ok = pcbnew.ExportSpecctraDSN(board, job["dsn"])
        return {"ok": bool(ok)}
    ok = pcbnew.ImportSpecctraSES(board, job["ses"])
    board.Save(job["out"])
    tracks = sum(1 for t in board.GetTracks() if t.GetClass() == "PCB_TRACK")
    vias = sum(1 for t in board.GetTracks() if t.GetClass() == "PCB_VIA")
    return {"ok": bool(ok), "tracks": tracks, "vias": vias}


if __name__ == "__main__":
    run(main)
