"""
State Manager for cross-session context preservation.

PoC Validation Target:
  Can a structured file-based state system effectively preserve context
  across Claude Code sessions, giving each new session enough information
  to continue where the previous one left off?

Design Decisions:
  - All state lives in a single JSON file so Claude Code can read/write it directly.
  - Atomic writes (tempfile + os.replace) prevent corruption on crash.
  - Git checkpoints before each session allow rollback if AI damages state.
  - create_session_context() produces a plain-text summary suitable for
    injection via SessionStart hook additionalContext.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class SessionRecord:
    """One completed session's outcome."""
    session_id: str
    mission: str
    started_at: str
    finished_at: str
    outcome: str          # "success" | "partial" | "failed"
    summary: str          # free-text summary of what happened
    friction: list[str] = field(default_factory=list)


@dataclass
class SupervisorState:
    """Full supervisor state persisted to disk between sessions."""

    # --- Purpose & missions ---
    current_mission: str | None = None
    mission_queue: list[str] = field(default_factory=list)
    completed_missions: list[str] = field(default_factory=list)

    # --- Observability ---
    friction_log: list[dict[str, str]] = field(default_factory=list)
    session_history: list[dict[str, Any]] = field(default_factory=list)

    # --- Metadata ---
    created_at: str = ""
    updated_at: str = ""
    session_counter: int = 0

    def __post_init__(self) -> None:
        now = _now_iso()
        if not self.created_at:
            self.created_at = now
        if not self.updated_at:
            self.updated_at = now


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_state_path() -> Path:
    return Path(__file__).parent / "state.json"


# ---------------------------------------------------------------------------
# Persistence (atomic read / write)
# ---------------------------------------------------------------------------

def save_state(state: SupervisorState, path: Path | None = None) -> Path:
    """Atomically write state to disk.

    Uses write-to-temp + os.replace so a crash mid-write never leaves a
    corrupt file. This is the simplest safe pattern that works on all
    platforms (POSIX rename is atomic within the same filesystem).
    """
    path = path or _default_state_path()
    state.updated_at = _now_iso()

    data = json.dumps(asdict(state), indent=2, ensure_ascii=False)

    # Write to a temp file in the same directory, then atomically replace.
    fd, tmp_path = tempfile.mkstemp(
        dir=path.parent,
        prefix=".state_",
        suffix=".tmp",
    )
    try:
        os.write(fd, data.encode("utf-8"))
        os.close(fd)
        os.replace(tmp_path, path)
    except BaseException:
        # Clean up temp file on any failure.
        os.close(fd) if not os.get_inheritable(fd) else None
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    return path


def load_state(path: Path | None = None) -> SupervisorState:
    """Load state from disk. Returns a fresh state if the file is missing."""
    path = path or _default_state_path()

    if not path.exists():
        return SupervisorState()

    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    return SupervisorState(**raw)


# ---------------------------------------------------------------------------
# Context generation (for SessionStart hook injection)
# ---------------------------------------------------------------------------

def create_session_context(state: SupervisorState) -> str:
    """Generate a plain-text context summary for a new Claude Code session.

    This text is designed to be injected as additionalContext via the
    SessionStart hook so the new session immediately knows:
      1. What was accomplished so far.
      2. What to do next.
      3. What friction has been observed.
    """
    lines: list[str] = []
    lines.append("=" * 60)
    lines.append("SUPERVISOR CONTEXT INJECTION")
    lines.append("=" * 60)

    # --- Last session ---
    if state.session_history:
        last = state.session_history[-1]
        lines.append("")
        lines.append("## Last Session")
        lines.append(f"  Mission:  {last.get('mission', 'N/A')}")
        lines.append(f"  Outcome:  {last.get('outcome', 'N/A')}")
        lines.append(f"  Summary:  {last.get('summary', 'N/A')}")
        if last.get("friction"):
            lines.append(f"  Friction: {', '.join(last['friction'])}")
    else:
        lines.append("")
        lines.append("## Last Session")
        lines.append("  (no previous session)")

    # --- Current mission ---
    lines.append("")
    lines.append("## Current Mission")
    if state.current_mission:
        lines.append(f"  {state.current_mission}")
    elif state.mission_queue:
        lines.append(f"  Next in queue: {state.mission_queue[0]}")
    else:
        lines.append("  (no missions remaining)")

    # --- Queue ---
    lines.append("")
    lines.append("## Mission Queue")
    if state.mission_queue:
        for i, m in enumerate(state.mission_queue, 1):
            lines.append(f"  {i}. {m}")
    else:
        lines.append("  (empty)")

    # --- Completed ---
    lines.append("")
    lines.append("## Completed Missions")
    if state.completed_missions:
        for m in state.completed_missions:
            lines.append(f"  - {m}")
    else:
        lines.append("  (none yet)")

    # --- Friction ---
    lines.append("")
    lines.append("## Friction Log (recent)")
    recent_friction = state.friction_log[-5:]  # last 5 entries
    if recent_friction:
        for entry in recent_friction:
            lines.append(f"  [{entry.get('time', '?')}] {entry.get('description', '?')}")
    else:
        lines.append("  (no friction recorded)")

    lines.append("")
    lines.append("=" * 60)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------

def advance_mission(state: SupervisorState) -> str | None:
    """Pop the next mission from the queue and set it as current.

    Returns the mission string, or None if the queue is empty.
    """
    if state.mission_queue:
        state.current_mission = state.mission_queue.pop(0)
        return state.current_mission
    state.current_mission = None
    return None


def record_session_result(
    state: SupervisorState,
    *,
    outcome: str,
    summary: str,
    friction: list[str] | None = None,
) -> None:
    """Update state after a session completes.

    - Archives the current mission into completed_missions (on success).
    - Appends a SessionRecord to session_history.
    - Logs any friction items.
    - Advances to the next mission.
    """
    state.session_counter += 1
    session_id = f"session-{state.session_counter:04d}"
    now = _now_iso()

    record = SessionRecord(
        session_id=session_id,
        mission=state.current_mission or "(none)",
        started_at=state.session_history[-1].get("started_at", now) if state.session_history else now,
        finished_at=now,
        outcome=outcome,
        summary=summary,
        friction=friction or [],
    )
    state.session_history.append(asdict(record))

    # Log friction
    if friction:
        for item in friction:
            state.friction_log.append({
                "time": now,
                "session": session_id,
                "description": item,
            })

    # Move mission to completed (only on success/partial)
    if outcome in ("success", "partial") and state.current_mission:
        state.completed_missions.append(state.current_mission)

    # Advance to next mission
    advance_mission(state)


# ---------------------------------------------------------------------------
# Git checkpoint
# ---------------------------------------------------------------------------

def create_git_checkpoint(
    state_path: Path | None = None,
    message: str | None = None,
) -> bool:
    """Create a git commit as a recovery checkpoint.

    Returns True if the commit succeeded, False otherwise.
    This is a best-effort operation -- failures are logged but not fatal.
    """
    state_path = state_path or _default_state_path()
    repo_root = state_path.parent

    if message is None:
        message = f"checkpoint: state at {_now_iso()}"

    try:
        subprocess.run(
            ["git", "add", str(state_path)],
            cwd=repo_root,
            capture_output=True,
            check=True,
        )
        result = subprocess.run(
            ["git", "commit", "-m", message, "--allow-empty"],
            cwd=repo_root,
            capture_output=True,
        )
        return result.returncode == 0
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        print(f"[checkpoint] git checkpoint failed: {exc}")
        return False


# ---------------------------------------------------------------------------
# Convenience: one-shot context dump to stdout (used by session_hook.sh)
# ---------------------------------------------------------------------------

def main() -> None:
    """CLI entry point: load state and print session context to stdout."""
    state = load_state()
    print(create_session_context(state))


if __name__ == "__main__":
    main()
