#!/bin/bash
# SessionStart hook: resolve the interpreter once, mark the session start, check boardforge,
# print the pipeline status. Plain script, no model calls.
set -uo pipefail

# Hook input (JSON on stdin) carries session_id; read it before anything else does.
INPUT="$(cat 2>/dev/null || true)"

# Per-session state shared with the other hooks. Keep this path rule identical to
# run-hook.sh, post-write.sh, kicadlibs.py and stop-check.py.
STATE_DIR="${TMPDIR:-/tmp}/boardforge-plugin"
mkdir -p "$STATE_DIR" 2>/dev/null

# Resolve the real Python interpreter ONCE. Version-manager shims (pyenv, asdf) cost
# ~0.5 s per call, and run-hook.sh starts Python on every tool call.
if REAL_PY="$(python3 -c 'import sys; print(sys.executable)' 2>/dev/null)" && [ -x "$REAL_PY" ]; then
    printf '%s\n' "$REAL_PY" > "$STATE_DIR/python"
else
    REAL_PY="python3"
fi

# Session-start marker: stop-check.py compares circuit-file mtimes against it. Only created
# on a fresh start, so a resumed session keeps its original marker.
SESSION_ID="$(printf '%s' "$INPUT" | "$REAL_PY" -c \
    'import sys, json; print(json.load(sys.stdin).get("session_id", ""))' 2>/dev/null)"
if [ -n "$SESSION_ID" ] && [ ! -f "$STATE_DIR/$SESSION_ID.start" ]; then
    touch "$STATE_DIR/$SESSION_ID.start"
fi

echo "=== boardforge plugin ==="
if ! command -v boardforge >/dev/null 2>&1; then
    echo "⚠️  boardforge is not on PATH: install it (uv tool install boardforge), then run boardforge doctor"
else
    # Prime the KiCad library-path cache the lookup scripts and hooks use.
    "$REAL_PY" -c "import sys; sys.path.insert(0, '$(dirname "$0")'); import kicadlibs; \
d = kicadlibs.kicad_dirs(refresh=True); print('✓ KiCad libraries:', d['symbols'] or 'NOT FOUND (boardforge doctor)')" 2>/dev/null
fi

if [ -f "design_state.json" ]; then
    "$REAL_PY" -c '
import json
s = json.load(open("design_state.json"))
r = s.get("review_status") or {}
print("📋 Circuit : {}".format(s.get("circuit_name", "unnamed")))
print("   Stage   : {}".format(s.get("stage", "unknown")))
print("   Review  : {}".format(r.get("status") or "not run"))
print("   Handoff : {}".format(s.get("last_handoff") or "none — phase not gated"))
print("   Cache   : {}".format("off (blank slate)" if s.get("use_cache") is False else "on"))
' 2>/dev/null || echo "  (design_state.json unreadable)"
else
    echo "No design_state.json here — /new-circuit starts a design"
fi
[ -f boardforge.toml ] && command -v boardforge >/dev/null 2>&1 && boardforge status 2>/dev/null | sed 's/^/   /'
echo "=== ready ==="
