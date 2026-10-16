"""pipeline_state.json: what each stage did, so a run can resume and be inspected."""

import json
import os
import time

STAGES = ("c1", "c2", "c3", "c4", "c5", "c6")
TITLES = {
    "c1": "schematic + netlist parity",
    "c2": "board setup",
    "c3": "placement",
    "c4": "routing",
    "c5": "checks",
    "c6": "fab outputs",
}


class State:
    def __init__(self, path):
        self.path = path
        self.data = {"stages": {}}
        if os.path.exists(path):
            with open(path) as f:
                self.data = json.load(f)

    def save(self):
        with open(self.path, "w") as f:
            json.dump(self.data, f, indent=1)

    def stage(self, sid):
        return self.data["stages"].get(sid, {})

    def record(self, sid, status, summary, warnings=(), seconds=None):
        """status: done | warn | failed."""
        self.data["stages"][sid] = {
            "status": status,
            "title": TITLES.get(sid, sid),
            "summary": summary,
            "warnings": list(warnings),
            "seconds": seconds,
            "finished": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        # A rerun of an earlier stage makes the later ones stale.
        later = STAGES[STAGES.index(sid) + 1:] if sid in STAGES else ()
        for s in later:
            if s in self.data["stages"]:
                self.data["stages"][s]["status"] = "stale"
        self.save()

    def next_stage(self):
        for s in STAGES:
            if self.stage(s).get("status") not in ("done", "warn"):
                return s
        return None
