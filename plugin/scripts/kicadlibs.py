"""Where the KiCad libraries are, for the plugin's lookup scripts (a module, not a tool).

boardsmith finds KiCad 10 itself; this asks it once (`boardsmith doctor`) and caches the
answer per machine in ${TMPDIR:-/tmp}/boardsmith-plugin/kicad_dirs.json, so hooks that run
on every Write stay fast. $KICAD10_SYMBOL_DIR / $KICAD10_FOOTPRINT_DIR override it.
Project libraries come from the project's own directories: symbols/ and the
[libraries.footprints] table of boardsmith.toml.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

CACHE = Path(os.environ.get("TMPDIR") or "/tmp") / "boardsmith-plugin" / "kicad_dirs.json"


def kicad_dirs(refresh: bool = False) -> dict:
    """{"symbols": dir or "", "footprints": dir or ""}."""
    found = {}
    if CACHE.is_file() and not refresh:
        try:
            found = json.loads(CACHE.read_text())
        except (OSError, json.JSONDecodeError):
            found = {}
    if not found:
        try:
            out = subprocess.run(["boardsmith", "doctor"], capture_output=True, text=True,
                                 timeout=60, cwd=os.environ.get("TMPDIR") or "/tmp").stdout
        except (OSError, subprocess.TimeoutExpired):
            out = ""
        for line in out.splitlines():
            key, _, val = line.partition(": ")
            if key in ("symbols", "footprints"):
                found[key] = val.strip()
        if found:
            try:
                CACHE.parent.mkdir(parents=True, exist_ok=True)
                CACHE.write_text(json.dumps(found))
            except OSError:
                pass
    for key in ("symbols", "footprints"):
        env = os.environ.get(f"KICAD10_{key[:-1].upper()}_DIR")
        if env:
            found[key] = env
        found.setdefault(key, "")
    return found


def project_footprint_libs(root: Path | None = None) -> dict[str, Path]:
    """{nickname: .pretty dir} from boardsmith.toml [libraries.footprints], plus any
    footprints/<Lib>.pretty not listed there (reported, so it can be added)."""
    root = root or Path.cwd()
    libs: dict[str, Path] = {}
    toml = root / "boardsmith.toml"
    if toml.is_file():
        try:
            import tomllib

            cfg = tomllib.loads(toml.read_text())
            for nick, path in (cfg.get("libraries", {}).get("footprints", {}) or {}).items():
                libs[nick] = (root / os.path.expanduser(path)).resolve()
        except (ImportError, ValueError, OSError):
            pass
    local = root / "footprints"
    if local.is_dir():
        for d in local.glob("*.pretty"):
            libs.setdefault(d.stem, d)
    return libs
