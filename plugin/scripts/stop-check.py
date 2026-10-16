#!/usr/bin/env python3
"""Stop hook — warn when circuit code changed this session and `boardforge sch` hasn't run
since (or its last run failed or found ERC errors).

Warns when BOTH hold:
  * a `circuits/**/*.py` file was modified this session (after the marker that
    `session-start.sh` writes), and
  * C1 hasn't verified it: build/netlist.json is older than the newest circuit file, or
    boardforge's last C1 run (build/pipeline_state.json) failed or reported ERC errors.

A session that never touched circuit code is left alone: no circuit exists before stage 5.

This WARNS, it does not block. A blocking Stop hook (exit 2) with a condition that persists
(ERC genuinely failing) would force Claude to continue indefinitely, and the easiest way out
of that loop is faking a pass, which is what this hook exists to prevent. The phase 6 gate
itself is enforced by the pipeline.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

WARNING = ("Circuit code changed but `boardforge sch` hasn't verified it (or found ERC errors). "
           "Run `boardforge sch` and `boardforge review` before the physical pipeline.")


def marker_path(session_id: str) -> Path:
    # Same rule as session-start.sh (`${TMPDIR:-/tmp}`).
    return Path(os.environ.get("TMPDIR") or "/tmp") / "boardforge-plugin" / f"{session_id}.start"


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        data = {}
    root = Path(data.get("cwd") or ".")
    circuits = [p for p in (root / "circuits").rglob("*.py") if "__pycache__" not in p.parts]
    if not circuits:
        return 0
    newest = max(p.stat().st_mtime for p in circuits)

    marker = marker_path(data.get("session_id", ""))
    netlist = root / "build" / "netlist.json"
    built = netlist.stat().st_mtime if netlist.is_file() else None
    if marker.is_file():
        touched_this_session = newest > marker.stat().st_mtime
    else:
        touched_this_session = built is None or newest > built

    c1 = {}
    try:
        c1 = json.loads((root / "build" / "pipeline_state.json").read_text())["stages"].get("c1") or {}
    except (OSError, json.JSONDecodeError, KeyError, AttributeError):
        pass
    summary = c1.get("summary") or {}
    erc_errors = (summary.get("skidl_erc") or {}).get("errors", 0) + (summary.get("erc") or {}).get("errors", 0)
    unverified = built is None or newest > built or c1.get("status") == "failed" or erc_errors > 0
    if touched_this_session and unverified:
        print(json.dumps({"systemMessage": WARNING}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
