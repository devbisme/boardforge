"""Find KiCad 10 and run things with it.

Two kinds of KiCad work happen in subprocesses:
- ``kicad-cli`` commands (netlist, ERC, DRC, exports);
- board mutations in KiCad's own Python (``pcbnew``), one fresh process per step, which
  avoids stale SWIG wrappers after Remove()/Add() churn. Those scripts live in
  ``boardforge.pcb`` and talk to this side through a JSON job file and a JSON result file.
"""

import glob
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass

PKG_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIN_MAJOR = 10


class KicadError(RuntimeError):
    pass


def _expand(p):
    return os.path.abspath(os.path.expanduser(os.path.expandvars(p))) if p else ""


def _cli_version(cli):
    try:
        out = subprocess.run([cli, "version"], capture_output=True, text=True, timeout=60).stdout
        return out.strip().splitlines()[-1] if out.strip() else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _major(version):
    try:
        return int(version.split(".")[0])
    except ValueError:
        return 0


def _cli_candidates():
    yield os.environ.get("BOARDFORGE_KICAD_CLI", "")
    yield shutil.which("kicad-cli") or ""
    yield "/usr/bin/kicad-cli"
    yield "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"
    yield from glob.glob(r"C:\Program Files\KiCad\*\bin\kicad-cli.exe")
    # Extracted AppImages, e.g. ~/bin/kicad10-root/bin/kicad-cli.
    yield from sorted(glob.glob(os.path.expanduser("~/bin/kicad*/bin/kicad-cli")), reverse=True)


def _python_ok(py):
    try:
        r = subprocess.run([py, "-c", "import pcbnew, sys; print(pcbnew.Version())"],
                           capture_output=True, text=True, timeout=120)
        return r.returncode == 0 and r.stdout.strip() != ""
    except (OSError, subprocess.TimeoutExpired):
        return False


@dataclass
class Kicad:
    cli: str
    version: str
    python: str
    symbol_dir: str
    footprint_dir: str
    warnings: list

    @classmethod
    def find(cls, cfg):
        """Locate kicad-cli >= 10, a Python that imports its pcbnew, and its libraries.

        Config values (``[kicad]``) win; then BOARDFORGE_KICAD_CLI / BOARDFORGE_KICAD_PYTHON;
        then the usual install locations. A KiCad older than 10 is used with a warning.
        """
        warnings = []
        cli, version = "", ""
        cands = [_expand(cfg.get("cli", ""))] + [c for c in _cli_candidates()]
        fallback = None
        for c in cands:
            if not c or not os.path.exists(c):
                continue
            v = _cli_version(c)
            if _major(v) >= MIN_MAJOR:
                cli, version = c, v
                break
            fallback = fallback or (c, v)
        if not cli:
            if not fallback:
                raise KicadError("kicad-cli not found; set [kicad] cli or BOARDFORGE_KICAD_CLI")
            cli, version = fallback
            warnings.append(f"kicad-cli {version} at {cli} is older than {MIN_MAJOR}")
        bindir = os.path.dirname(cli)
        root = os.path.dirname(bindir)

        py = _expand(cfg.get("python", "")) or os.environ.get("BOARDFORGE_KICAD_PYTHON", "")
        if not py:
            cands = sorted(glob.glob(os.path.join(bindir, "python3*")))
            cands += glob.glob(os.path.join(root, "Frameworks/Python.framework/Versions/*/bin/python3"))
            cands += [os.path.join(bindir, "python.exe"), "/usr/bin/python3", sys.executable]
            py = next((c for c in cands if os.path.exists(c) and _python_ok(c)), "")
        if not py:
            raise KicadError("no Python that can import pcbnew; set [kicad] python")

        def libdir(key, sub, env):
            d = _expand(cfg.get(key, "")) or os.environ.get(env, "")
            for cand in (d, os.path.join(root, "share", "kicad", sub),
                         os.path.join(root, "SharedSupport", sub), f"/usr/share/kicad/{sub}"):
                if cand and os.path.isdir(cand):
                    return cand
            warnings.append(f"KiCad {sub} directory not found")
            return ""

        return cls(cli=cli, version=version, python=py,
                   symbol_dir=libdir("symbol_dir", "symbols", "KICAD10_SYMBOL_DIR"),
                   footprint_dir=libdir("footprint_dir", "footprints", "KICAD10_FOOTPRINT_DIR"),
                   warnings=warnings)

    def env(self):
        """Environment for KiCad subprocesses: library variables point at this KiCad's libs."""
        e = dict(os.environ)
        for var in ("KICAD10_SYMBOL_DIR", "KICAD_SYMBOL_DIR"):
            e[var] = self.symbol_dir
        for var in ("KICAD10_FOOTPRINT_DIR", "KICAD_FOOTPRINT_DIR"):
            e[var] = self.footprint_dir
        e["PYTHONPATH"] = PKG_PARENT + os.pathsep + e.get("PYTHONPATH", "")
        return e

    # ---- kicad-cli
    def cli_run(self, *args, log=None, check=False, timeout=1800):
        """Run kicad-cli; output goes to the log file (if given). Returns CompletedProcess."""
        r = subprocess.run([self.cli, *map(str, args)], capture_output=True, text=True,
                           env=self.env(), timeout=timeout)
        if log:
            with open(log, "a") as f:
                f.write(f"$ kicad-cli {' '.join(map(str, args))}\n{r.stdout}{r.stderr}\n")
        if check and r.returncode != 0:
            raise KicadError(f"kicad-cli {args[0]} {args[1]} failed: {r.stderr.strip()[-500:]}")
        return r

    # ---- pcbnew scripts
    def pcb(self, module, job, workdir, timeout=3600):
        """Run ``boardforge.pcb.<module>`` under KiCad's Python with ``job`` (a dict).

        The script writes its result dict to a JSON file; that dict is returned with
        ``seconds`` added. A crash returns {"ok": False, "error": ...} instead of raising,
        so callers can record it and continue.
        """
        os.makedirs(workdir, exist_ok=True)
        stamp = f"{module}_{int(time.time() * 1000) % 10**8}"
        job_file = os.path.join(workdir, stamp + ".job.json")
        res_file = os.path.join(workdir, stamp + ".result.json")
        log_file = os.path.join(workdir, "pcbnew.log")
        job = dict(job, result=res_file, footprint_dir=self.footprint_dir)
        with open(job_file, "w") as f:
            json.dump(job, f, indent=1)
        t0 = time.time()
        try:
            # Some KiCad Pythons (AppImage) reset sys.path and ignore PYTHONPATH, so the
            # package directory is put on sys.path explicitly.
            boot = (f"import sys, runpy; sys.path.insert(0, {PKG_PARENT!r}); "
                    f"runpy.run_module('boardforge.pcb.{module}', run_name='__main__')")
            r = subprocess.run([self.python, "-c", boot, job_file],
                               capture_output=True, text=True, env=self.env(), timeout=timeout)
            out = r.stdout + r.stderr
        except subprocess.TimeoutExpired as e:
            r, out = None, f"timeout after {timeout} s\n{e.stdout or ''}"
        with open(log_file, "a") as f:
            f.write(f"=== {module} {job_file}\n{_quiet(out)}\n")
        if os.path.exists(res_file):
            with open(res_file) as f:
                res = json.load(f)
        else:
            tail = "\n".join(_quiet(out).splitlines()[-15:])
            res = {"ok": False, "error": f"{module} produced no result (rc="
                   f"{getattr(r, 'returncode', None)}):\n{tail}"}
        res["seconds"] = round(time.time() - t0, 1)
        return res


def _quiet(text):
    """Drop KiCad's Python noise (leak reports, debug and window messages)."""
    drop = ("leak", "debug", "window", "gtk", "wx", "fontconfig")
    return "\n".join(l for l in text.splitlines() if not any(d in l.lower() for d in drop))
