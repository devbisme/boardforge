#!/bin/bash
# PostToolUse:Write hook — syntax + footprint check on circuit files.
# Exit 2 (with stderr) is the blocking status Claude reads back; exit 1 is only
# shown to the user, so failures here must use 2.
set -uo pipefail

# Interpreter resolved once by session-start.sh; avoids a version-manager shim's
# ~0.5 s start-up on each of the three Python calls below. Falls back to PATH.
PY="python3"
CACHE="${TMPDIR:-/tmp}/boardforge-plugin/python"
[ -r "$CACHE" ] && [ -x "$(cat "$CACHE")" ] && PY="$(cat "$CACHE")"

INPUT=$(cat)
FILE_PATH=$(printf '%s' "$INPUT" | "$PY" -c "
import sys, json
d = json.load(sys.stdin)
print(d.get('tool_input', {}).get('file_path') or d.get('path', ''))
" 2>/dev/null || echo "")

# Only act on circuit Python files
if [[ "$FILE_PATH" =~ circuits/.*\.py$ ]]; then
    echo "=== Post-write hook: Checking $FILE_PATH ==="

    if ! RESULT=$("$PY" -m py_compile "$FILE_PATH" 2>&1); then
        {
            echo "⚠️  SYNTAX ERROR in $FILE_PATH:"
            echo "$RESULT"
            echo "→ Fix required before continuing."
        } >&2
        exit 2
    fi

    echo "✓ $FILE_PATH syntax OK"

    # Footprint strings must resolve (KiCad's libraries + boardforge.toml [libraries.footprints])
    VALIDATE_SCRIPT="$(dirname "$0")/validate-footprints.py"
    if [ -f "$VALIDATE_SCRIPT" ]; then
        if ! FP_RESULT=$("$PY" "$VALIDATE_SCRIPT" "$FILE_PATH" 2>&1); then
            {
                echo "$FP_RESULT"
                echo "→ Fix footprints before continuing. Use generate-footprint.py for custom packages."
            } >&2
            exit 2
        fi
        echo "$FP_RESULT"
    fi
fi

exit 0
