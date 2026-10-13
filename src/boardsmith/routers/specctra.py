"""Base class for routers that read Specctra DSN and write SES.

A subclass only supplies ``command(dsn, ses, job)``. This class exports the DSN with KiCad's
converter, prepares it for plane handling, runs the command and imports the session.
Locked items (plane fanout, preroutes) go into the DSN as fixed wiring and come back
unchanged in the SES.
"""

import os
import re
import subprocess
import time

from boardsmith.routers.base import Router, RouteResult


def prepare_dsn(text, plane_nets, plane_layers):
    """Make a KiCad DSN safe for a router that doesn't use planes.

    - Plane nets are cut to a single pin: their pads are already fanned out to locked vias
      that land on the planes, so the router must not route them.
    - Plane layers are typed ``power`` so no signal is routed there.
    - Plane polygons on the outer layers are dropped (they would block F/B routing).
    Returns (text, notes).
    """
    notes = []
    for net in plane_nets:
        pat = re.compile(r'(\(net\s+"?' + re.escape(net) + r'"?\s*\(pins\s+)([^)]*)(\))')
        m = pat.search(text)
        if not m:
            notes.append(f"plane net {net} not in DSN")
            continue
        pins = m.group(2).split()
        text = text[: m.start()] + m.group(1) + pins[0] + m.group(3) + text[m.end():]
        notes.append(f"{net}: {len(pins)} pins -> 1")
    for layer in plane_layers:
        text = re.sub(r'(\(layer\s+"?' + re.escape(layer) + r'"?\s*\(type )signal\)', r"\1power)", text)
    text = re.sub(r"\(plane [^\n]*\((polygon (F|B)\.Cu)[^)]*\)\)\n?", "", text)
    return text, notes


class SpecctraRouter(Router):
    name = "specctra"

    def command(self, dsn, ses, job):
        raise NotImplementedError

    def route(self, job):
        k = job.kicad
        base = os.path.join(job.workdir, self.name)
        dsn, dsn_p, ses = base + ".dsn", base + "_prepared.dsn", base + ".ses"
        routed = base + "_routed.kicad_pcb"
        log = base + ".log"
        for f in (dsn, ses):
            if os.path.exists(f):
                os.remove(f)
        r = k.pcb("specctra", {"mode": "export", "pcb": job.board, "dsn": dsn}, job.workdir)
        if not r.get("ok") or not os.path.exists(dsn):
            return RouteResult(job.board, -1, [], {}, log, ok=False,
                               error=f"DSN export failed: {r.get('error', '')}")
        with open(dsn) as f:
            text = f.read()
        plane_layers = [l for l, role in job.layer_roles.items() if role in ("plane", "avoid")]
        text, notes = prepare_dsn(text, job.plane_nets, plane_layers)
        with open(dsn_p, "w") as f:
            f.write(text)
        t0 = time.time()
        try:
            with open(log, "w") as lf:
                subprocess.run(self.command(dsn_p, ses, job), stdout=lf, stderr=subprocess.STDOUT,
                               timeout=job.time_budget)
        except subprocess.TimeoutExpired:
            notes.append(f"router timed out after {job.time_budget} s")
        secs = round(time.time() - t0, 1)
        if not os.path.exists(ses):
            return RouteResult(job.board, -1, [], {"seconds": secs}, log, ok=False,
                               error=f"{self.name} wrote no session file (see {log})")
        r = k.pcb("specctra", {"mode": "import", "pcb": job.board, "ses": ses, "out": routed},
                  job.workdir)
        if not r.get("ok"):
            return RouteResult(job.board, -1, [], {"seconds": secs}, log, ok=False,
                               error=f"SES import failed: {r.get('error', '')}")
        return self._measure(job, routed, {"seconds": secs, "tracks": r.get("tracks"),
                                           "vias": r.get("vias"), "notes": notes}, log)
