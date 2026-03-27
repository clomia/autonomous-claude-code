"""
Multi-session integration test for the state persistence PoC.

This script validates the core hypothesis:
  CLAUDE.md (persistent instructions) + state files (dynamic context) +
  SessionStart hooks (context injection) can effectively replace
  conversation memory across independent Claude Code sessions.

Test Plan:
  1. Create initial state with 3 simple missions.
  2. Run Claude Code session 1 with the first mission.
  3. Update state with session 1 results.
  4. Run Claude Code session 2 (fresh session, NOT --continue).
  5. Verify session 2 picks up from where session 1 left off.

Usage:
  python3 run_multi_session.py           # full run (requires Claude Code CLI)
  python3 run_multi_session.py --dry-run # validate state logic without launching Claude Code
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import textwrap
from pathlib import Path

from state_manager import (
    SupervisorState,
    advance_mission,
    create_git_checkpoint,
    create_session_context,
    load_state,
    record_session_result,
    save_state,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

POC_DIR = Path(__file__).parent.resolve()
STATE_PATH = POC_DIR / "state.json"
SETTINGS_PATH = POC_DIR / "settings.json"

# Simple missions that Claude Code can complete without external dependencies.
INITIAL_MISSIONS = [
    "Create a file called 'output/session1.txt' with a haiku about programming",
    "Create a file called 'output/session2.txt' that references the content of session1.txt and adds a response haiku",
    "Create a file called 'output/session3.txt' summarizing what was accomplished across all sessions",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def banner(text: str) -> None:
    width = 70
    print()
    print("=" * width)
    print(f"  {text}")
    print("=" * width)
    print()


def run_claude_session(
    prompt: str,
    *,
    session_label: str = "",
    timeout_seconds: int = 120,
) -> dict:
    """Launch a single Claude Code session and return the parsed JSON result.

    Key flags:
      --output-format json       structured output for programmatic consumption
      --dangerously-skip-permissions  unattended execution (PoC only)
      -p <prompt>                non-interactive single-prompt mode
    """
    cmd = [
        "claude",
        "-p", prompt,
        "--output-format", "json",
        "--dangerously-skip-permissions",
        "--model", "opus",
    ]

    banner(f"RUNNING CLAUDE SESSION: {session_label}")
    print(f"  Prompt (truncated): {prompt[:120]}...")
    print(f"  Working dir: {POC_DIR}")
    print()

    try:
        result = subprocess.run(
            cmd,
            cwd=str(POC_DIR),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        print(f"  [TIMEOUT] Session {session_label} exceeded {timeout_seconds}s")
        return {"error": "timeout", "session": session_label}

    print(f"  Exit code: {result.returncode}")

    if result.returncode != 0:
        print(f"  STDERR: {result.stderr[:500]}")
        return {
            "error": "non-zero exit",
            "exit_code": result.returncode,
            "stderr": result.stderr[:1000],
            "session": session_label,
        }

    # Try to parse JSON output.
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError:
        # Claude Code may emit non-JSON preamble; try to find the JSON object.
        parsed = {"raw_output": result.stdout[:2000], "session": session_label}

    return parsed


def extract_result_text(claude_output: dict) -> str:
    """Best-effort extraction of the textual result from Claude Code JSON output."""
    # The JSON output format varies; handle common shapes.
    if "result" in claude_output:
        return str(claude_output["result"])
    if "raw_output" in claude_output:
        return str(claude_output["raw_output"])
    return json.dumps(claude_output, indent=2)


# ---------------------------------------------------------------------------
# Main test flow
# ---------------------------------------------------------------------------

def setup_initial_state() -> SupervisorState:
    """Create and persist the initial state with 3 missions."""
    banner("SETUP: Creating initial state")

    state = SupervisorState(
        mission_queue=list(INITIAL_MISSIONS),
    )
    # Advance to the first mission.
    advance_mission(state)
    save_state(state, STATE_PATH)

    print(f"  State file: {STATE_PATH}")
    print(f"  Current mission: {state.current_mission}")
    print(f"  Queue: {state.mission_queue}")
    print()

    return state


def run_session(state: SupervisorState, session_num: int, dry_run: bool = False) -> SupervisorState:
    """Run one session cycle: prepare -> execute -> record -> save."""
    banner(f"SESSION {session_num}")

    # Show what context the hook would inject.
    context = create_session_context(state)
    print("--- Context that SessionStart hook would inject ---")
    print(context)
    print("--- End context ---\n")

    if not state.current_mission:
        print("  No current mission. All done!")
        return state

    prompt = textwrap.dedent(f"""\
        You are executing a mission as part of an autonomous system.
        Read the state file at {STATE_PATH} for full context.

        Your current mission: {state.current_mission}

        After completing the mission:
        1. Verify the output exists and is correct.
        2. Report what you did in a brief summary.
    """).strip()

    if dry_run:
        print(f"  [DRY RUN] Would run Claude Code with prompt:")
        print(f"    {prompt[:200]}...")
        outcome = "success"
        summary = f"[dry-run] Simulated completion of: {state.current_mission}"
        friction: list[str] = []
    else:
        # Ensure output directory exists.
        (POC_DIR / "output").mkdir(exist_ok=True)

        # Create git checkpoint before session.
        save_state(state, STATE_PATH)
        create_git_checkpoint(STATE_PATH, f"checkpoint: before session {session_num}")

        # Run the actual Claude Code session.
        result = run_claude_session(
            prompt,
            session_label=f"session-{session_num}",
            timeout_seconds=180,
        )

        result_text = extract_result_text(result)
        print(f"\n  Claude output (truncated): {result_text[:300]}...\n")

        # Determine outcome.
        if "error" in result:
            outcome = "failed"
            summary = f"Session failed: {result.get('error')}"
            friction = [f"Session {session_num} failed: {result.get('error')}"]
        else:
            outcome = "success"
            summary = result_text[:500]
            friction = []

    # Record result and advance.
    record_session_result(
        state,
        outcome=outcome,
        summary=summary,
        friction=friction,
    )
    save_state(state, STATE_PATH)

    print(f"  Outcome: {outcome}")
    print(f"  Next mission: {state.current_mission or '(none)'}")
    print(f"  Completed: {state.completed_missions}")

    return state


def verify_continuity(state: SupervisorState) -> bool:
    """Check that state correctly reflects multi-session progression."""
    banner("VERIFICATION")

    checks: list[tuple[str, bool]] = []

    # Check 1: At least 2 sessions recorded.
    checks.append((
        "At least 2 sessions recorded",
        len(state.session_history) >= 2,
    ))

    # Check 2: At least 2 missions completed.
    checks.append((
        "At least 2 missions completed",
        len(state.completed_missions) >= 2,
    ))

    # Check 3: Session history has sequential IDs.
    if len(state.session_history) >= 2:
        ids = [s.get("session_id", "") for s in state.session_history]
        checks.append((
            "Session IDs are sequential",
            ids == sorted(ids),
        ))
    else:
        checks.append(("Session IDs are sequential", False))

    # Check 4: Mission queue + current_mission accounts for unfinished work.
    # After N sessions, N missions are completed, 1 may be in current_mission,
    # and the rest sit in mission_queue.
    total_unfinished = len(state.mission_queue) + (1 if state.current_mission else 0)
    expected_unfinished = max(0, len(INITIAL_MISSIONS) - len(state.completed_missions))
    checks.append((
        f"Unfinished missions ({total_unfinished}) matches expected ({expected_unfinished})",
        total_unfinished == expected_unfinished,
    ))

    # Check 5: Context generation works for current state.
    ctx = create_session_context(state)
    checks.append((
        "Context generation produces non-empty output",
        len(ctx) > 100,
    ))

    # Report.
    all_passed = True
    for label, passed in checks:
        status = "PASS" if passed else "FAIL"
        print(f"  [{status}] {label}")
        if not passed:
            all_passed = False

    print()
    if all_passed:
        print("  All checks passed. State persistence pattern validated.")
    else:
        print("  Some checks failed. Review output above.")

    return all_passed


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Multi-session state persistence PoC test",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate state logic without launching Claude Code",
    )
    parser.add_argument(
        "--sessions",
        type=int,
        default=2,
        help="Number of sessions to run (default: 2)",
    )
    args = parser.parse_args()

    # Step 1: Setup.
    state = setup_initial_state()

    # Step 2: Run N sessions.
    for i in range(1, args.sessions + 1):
        state = run_session(state, session_num=i, dry_run=args.dry_run)

    # Step 3: Verify.
    success = verify_continuity(state)

    # Final state dump.
    banner("FINAL STATE")
    final_context = create_session_context(state)
    print(final_context)

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
