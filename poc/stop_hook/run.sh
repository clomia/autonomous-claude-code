#!/usr/bin/env bash
#
# run.sh — Launch the Stop-hook Ralph Loop PoC
#
# This script:
#   1. Resets state from any previous run
#   2. Creates a temporary working directory
#   3. Copies the task file and CLAUDE.md into it
#   4. Launches Claude Code with the custom settings and permissions skipped

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK_DIR="$(mktemp -d -t ralph-loop-poc-XXXXXX)"

echo "=================================================="
echo "  Ralph Loop PoC — Stop Hook Pattern"
echo "=================================================="
echo ""
echo "Script dir : $SCRIPT_DIR"
echo "Work dir   : $WORK_DIR"
echo ""

# ---------------------------------------------------------------------------
# 1. Reset state from previous runs
# ---------------------------------------------------------------------------
rm -f "$SCRIPT_DIR/.iteration_counter"

# Restore tasks.json to all-pending state
cat > "$SCRIPT_DIR/tasks.json" <<'TASKS'
{
  "tasks": [
    {
      "id": "task-1",
      "description": "Create a file called hello.txt containing 'Hello from task 1'",
      "status": "pending"
    },
    {
      "id": "task-2",
      "description": "Create a file called world.txt containing 'Hello from task 2'",
      "status": "pending"
    },
    {
      "id": "task-3",
      "description": "Create a file called summary.txt listing all completed tasks",
      "status": "pending"
    }
  ]
}
TASKS

echo "[setup] State reset: counter cleared, tasks.json restored"
echo ""

# ---------------------------------------------------------------------------
# 2. Prepare the working directory
# ---------------------------------------------------------------------------
cp "$SCRIPT_DIR/CLAUDE.md" "$WORK_DIR/CLAUDE.md"
echo "[setup] Copied CLAUDE.md to $WORK_DIR"
echo ""

# ---------------------------------------------------------------------------
# 3. Launch Claude Code
# ---------------------------------------------------------------------------
echo "[launch] Starting Claude Code..."
echo "         Settings : $SCRIPT_DIR/settings.json"
echo "         CWD      : $WORK_DIR"
echo ""

claude \
    --dangerously-skip-permissions \
    --settings-file "$SCRIPT_DIR/settings.json" \
    --print \
    --prompt "Read the CLAUDE.md file for instructions, then begin working through the task queue. The tasks.json file is located at $SCRIPT_DIR/tasks.json" \
    2>&1

EXIT_CODE=$?

# ---------------------------------------------------------------------------
# 4. Report results
# ---------------------------------------------------------------------------
echo ""
echo "=================================================="
echo "  PoC Complete (exit code: $EXIT_CODE)"
echo "=================================================="
echo ""

echo "--- Final tasks.json ---"
cat "$SCRIPT_DIR/tasks.json"
echo ""

echo "--- Iteration counter ---"
if [ -f "$SCRIPT_DIR/.iteration_counter" ]; then
    echo "Iterations: $(cat "$SCRIPT_DIR/.iteration_counter")"
else
    echo "Counter file not found (hook may not have fired)"
fi
echo ""

echo "--- Files created in work dir ---"
ls -la "$WORK_DIR"
echo ""

echo "Work dir preserved at: $WORK_DIR"
echo "(clean up manually when done)"
