#!/usr/bin/env python3
"""Check that KiCad footprint strings resolve to a .kicad_mod (KiCad's libraries and the
project's [libraries.footprints]), with close-match suggestions.

Usage:
    validate-footprints.py <circuit.py> | <circuit_dir/>      footprint= strings in code
    validate-footprints.py Lib:Name [Lib:Name ...]            strings on the command line
    validate-footprints.py footprints.txt                     one Lib:Name per line

Exit codes:
    0 — all footprints valid
    1 — one or more footprints not found
    2 — usage error
"""
import sys
import re
import os
import pathlib
import difflib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kicadlibs import kicad_dirs, project_footprint_libs  # noqa: E402


def get_libraries():
    """{nickname: .pretty dir}: KiCad's stock libraries, then the project's (which win)."""
    stock = kicad_dirs()["footprints"]
    libs = {}
    if stock and os.path.isdir(stock):
        libs = {d.stem: d for d in pathlib.Path(stock).glob("*.pretty")}
    libs.update(project_footprint_libs())
    return libs


def extract_footprints(filepath):
    """Return list of (line_number, footprint_string) from a .py file."""
    results = []
    try:
        text = pathlib.Path(filepath).read_text()
    except (OSError, UnicodeDecodeError):
        return results
    for i, line in enumerate(text.splitlines(), 1):
        for m in re.finditer(r"""footprint\s*=\s*['"]([^'"]+)['"]""", line):
            results.append((i, m.group(1)))
    return results


def validate(footprint_str, libs):
    """Check that Library:Name resolves to <lib>.pretty/<Name>.kicad_mod.

    Returns (ok: bool, message: str).
    """
    if ":" not in footprint_str:
        return False, "Invalid format — expected 'Library:FootprintName'"
    lib, name = footprint_str.split(":", 1)
    if lib in libs:
        if (libs[lib] / f"{name}.kicad_mod").exists():
            return True, "OK"
        existing = [f.stem for f in libs[lib].glob("*.kicad_mod")]
        close = difflib.get_close_matches(name, existing, n=3, cutoff=0.5)
        if close:
            return False, f"Not found — did you mean: {', '.join(close)}"
        return False, f"Not found in {lib}.pretty (no close matches found)"
    close_libs = difflib.get_close_matches(lib, list(libs), n=2, cutoff=0.6)
    if close_libs:
        return False, f"Library '{lib}' not found — close library names: {', '.join(close_libs)}"
    return False, (f"Library '{lib}' not found (a project library goes in boardsmith.toml "
                   "[libraries.footprints])")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    items = []  # (where, footprint string)
    for arg in sys.argv[1:]:
        target = pathlib.Path(arg)
        if ":" in arg and not target.exists():
            items.append(("arg", arg))
        elif target.is_dir() or target.suffix == ".py":
            files = sorted(target.glob("**/*.py")) if target.is_dir() else [target]
            for f in files:
                if f.name != "__init__.py":
                    items += [(f"{f.name}:{n}", fp) for n, fp in extract_footprints(f)]
        elif target.is_file():
            items += [(f"{target.name}:{n}", line.strip())
                      for n, line in enumerate(target.read_text().splitlines(), 1)
                      if line.strip() and not line.startswith("#")]
        else:
            print(f"Error: '{arg}' is neither a file nor a Library:Name string")
            sys.exit(2)

    libs = get_libraries()
    if not libs:
        print("Warning: no footprint libraries found (is boardsmith installed? `boardsmith doctor`)"
              " — skipping validation")
        sys.exit(0)

    errors = [(where, fp, msg) for where, fp in items for ok, msg in [validate(fp, libs)] if not ok]
    if errors:
        print(f"FOOTPRINT VALIDATION FAILED — {len(errors)} error(s) ({len(items)} checked):")
        for where, fp, msg in errors:
            print(f"  ✗ {where}: '{fp}' — {msg}")
        sys.exit(1)
    print(f"✓ All footprint strings resolve ({len(items)} checked)")
    print("  Resolution only. Pad count, pitch and body against the datasheet, and the body")
    print("  variant against the BOM, are `boardsmith review` (PKG:<MPN>, BOM).")


if __name__ == "__main__":
    main()
