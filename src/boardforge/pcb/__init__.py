"""Board scripts that run under KiCad's Python (``python -m boardforge.pcb.<name> job.json``).

Each script loads the board, does one step, saves, and writes a JSON result. They import
only the standard library, ``pcbnew`` and the pure-Python parts of boardforge
(``boardforge.netlist``), because KiCad's Python has none of boardforge's dependencies.
All board-mutation code lives here, so retargeting from SWIG to KiCad's IPC API (design
doc, risks) touches only this package.
"""
