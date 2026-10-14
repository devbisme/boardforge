"""Freerouting (https://github.com/freerouting/freerouting) as the main router.

Prefers the self-contained binary (bundled Java runtime, no Java version trap); a jar is run
with ``java -jar``. Run headless with ``-mt 1`` by default: multithreaded optimization was
unreliable in the runs.

Neck-down is off by default: Freerouting necks tracks at small pads and retries failed
connections at 0.1 mm, which can fall below the design's or the fab's minimum width. The A*
finisher routes what Freerouting can't reach at full width (with a fallback to the default
width). Freerouting still thins some wires near pins (to 0.75-0.9 of the width); DRC flags
those below the board minimum and the A* finisher repairs them.
"""

import glob
import os
import shutil

from boardsmith.routers.specctra import SpecctraRouter


def find_freerouting(cfg):
    cands = [cfg.get("path", ""), os.environ.get("BOARDSMITH_FREEROUTING", ""),
             shutil.which("freerouting") or ""]
    cands += sorted(glob.glob(os.path.expanduser("~/bin/freerouting/*/*/bin/freerouting")), reverse=True)
    cands += sorted(glob.glob(os.path.expanduser("~/.local/share/boardsmith/freerouting/*/bin/freerouting")),
                    reverse=True)
    for c in cands:
        c = os.path.expanduser(c) if c else ""
        if c and os.path.exists(c):
            return c
    return ""


class Freerouting(SpecctraRouter):
    name = "freerouting"

    def capabilities(self):
        return dict(super().capabilities(), incremental=True, multithreaded=True)

    def available(self, cfg):
        p = find_freerouting(cfg.get("routing", {}).get("freerouting", {}))
        return (bool(p), p or "not found: set [routing.freerouting] path or BOARDSMITH_FREEROUTING")

    def command(self, dsn, ses, job):
        exe = find_freerouting(job.options)
        if not exe:
            raise FileNotFoundError("Freerouting not found")
        cmd = ["java", "-jar", exe] if exe.endswith(".jar") else [exe]
        # The GUI setting comes from ~/.config/freerouting/freerouting.json; left on (with a
        # display), Freerouting can exit after routing without writing the session file.
        cmd += ["-de", dsn, "-do", ses, "-mp", str(job.options.get("max_passes", 40)),
                "-mt", str(job.threads), "--gui.enabled=false"]
        if not job.options.get("neckdown"):
            # Two separate mechanisms: neck-down at pins, and retrying a failed connection at
            # neck_width_um (default 100). Retries are kept at the default track width.
            cmd += ["--router.automatic_neckdown=false",
                    f"--router.neck_width_um={round(job.default_rule[0] * 1000)}"]
        return cmd
