#!/usr/bin/env bash
# SessionStart hook for cross-session context injection.
#
# This script is invoked by Claude Code at session start.
# It reads the current supervisor state file and outputs a plain-text
# context summary to stdout. Claude Code captures this output and
# injects it as additionalContext for the new session.
#
# The context includes:
#   - What was done in the last session
#   - What the current mission is
#   - Any friction recorded so far
#
# Usage in settings.json:
#   "hooks": { "SessionStart": [{ "type": "command", "command": "bash .../session_hook.sh" }] }

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_FILE="${SCRIPT_DIR}/state.json"

# ---- Guard: if no state file exists, output minimal context ----
if [[ ! -f "${STATE_FILE}" ]]; then
    echo "============================================================"
    echo "SUPERVISOR CONTEXT INJECTION"
    echo "============================================================"
    echo ""
    echo "## Status"
    echo "  No state file found at ${STATE_FILE}."
    echo "  This is likely the first session. Check CLAUDE.md for instructions."
    echo ""
    echo "============================================================"
    exit 0
fi

# ---- Delegate to Python state_manager for full context generation ----
# This keeps the logic in one place and avoids reimplementing JSON parsing in bash.
python3 "${SCRIPT_DIR}/state_manager.py"
