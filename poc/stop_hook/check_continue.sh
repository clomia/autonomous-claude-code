#!/usr/bin/env bash
#
# check_continue.sh — Stop hook script for the Ralph Loop PoC
#
# This script is invoked by Claude Code's Stop hook mechanism.
# It receives JSON on stdin describing the stop event and decides
# whether to allow Claude to stop or to block and inject new context.
#
# Safety: hard cap of 3 iterations to prevent runaway loops.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TASKS_FILE="$SCRIPT_DIR/tasks.json"
COUNTER_FILE="$SCRIPT_DIR/.iteration_counter"
MAX_ITERATIONS=3

# ---------------------------------------------------------------------------
# 1. Read the hook input from stdin
# ---------------------------------------------------------------------------
HOOK_INPUT="$(cat)"

# ---------------------------------------------------------------------------
# 2. Check for the stop_hook_active flag (infinite-loop guard)
#    When Claude is already responding to a hook-injected continuation,
#    the runtime sets stop_hook_active=true. We MUST allow stopping in
#    that case to avoid an infinite block loop.
# ---------------------------------------------------------------------------
STOP_HOOK_ACTIVE="$(echo "$HOOK_INPUT" | python3 -c "
import sys, json
data = json.load(sys.stdin)
print(str(data.get('stop_hook_active', False)).lower())
" 2>/dev/null || echo "false")"

if [ "$STOP_HOOK_ACTIVE" = "true" ]; then
    echo '{"decision": "allow"}'
    exit 0
fi

# ---------------------------------------------------------------------------
# 3. Track iteration count
# ---------------------------------------------------------------------------
if [ -f "$COUNTER_FILE" ]; then
    ITERATION=$(cat "$COUNTER_FILE")
else
    ITERATION=0
fi

ITERATION=$((ITERATION + 1))
echo "$ITERATION" > "$COUNTER_FILE"

# ---------------------------------------------------------------------------
# 4. Hard safety cap — always allow stopping after MAX_ITERATIONS
# ---------------------------------------------------------------------------
if [ "$ITERATION" -ge "$MAX_ITERATIONS" ]; then
    echo '{"decision": "allow"}'
    exit 0
fi

# ---------------------------------------------------------------------------
# 5. Check for pending tasks in tasks.json
# ---------------------------------------------------------------------------
if [ ! -f "$TASKS_FILE" ]; then
    echo '{"decision": "allow"}'
    exit 0
fi

PENDING_TASKS="$(python3 -c "
import json, sys

with open('$TASKS_FILE') as f:
    data = json.load(f)

pending = [t for t in data.get('tasks', []) if t.get('status') != 'done']
if not pending:
    print('NONE')
else:
    # Build a summary of the next pending task
    task = pending[0]
    print(json.dumps({
        'pending_count': len(pending),
        'next_task': task
    }))
" 2>/dev/null || echo "NONE")"

# ---------------------------------------------------------------------------
# 6. Decide: block or allow
# ---------------------------------------------------------------------------
if [ "$PENDING_TASKS" = "NONE" ]; then
    # All tasks are done (or tasks file is missing/broken) — let Claude stop
    echo '{"decision": "allow"}'
else
    PENDING_COUNT="$(echo "$PENDING_TASKS" | python3 -c "import sys,json; print(json.load(sys.stdin)['pending_count'])")"
    NEXT_ID="$(echo "$PENDING_TASKS" | python3 -c "import sys,json; print(json.load(sys.stdin)['next_task']['id'])")"
    NEXT_DESC="$(echo "$PENDING_TASKS" | python3 -c "import sys,json; print(json.load(sys.stdin)['next_task']['description'])")"

    # Block stopping and inject instructions for the next task
    CONTEXT="There are ${PENDING_COUNT} task(s) remaining in tasks.json. "
    CONTEXT+="Continue with task '${NEXT_ID}': ${NEXT_DESC}. "
    CONTEXT+="After completing it, update its status to 'done' in tasks.json. "
    CONTEXT+="This is iteration ${ITERATION}/${MAX_ITERATIONS}."

    # Emit the block decision with additionalContext
    python3 -c "
import json
print(json.dumps({
    'decision': 'block',
    'reason': 'Pending tasks remain in the queue (iteration ${ITERATION}/${MAX_ITERATIONS})',
    'additionalContext': '''${CONTEXT}'''
}))
"
fi
