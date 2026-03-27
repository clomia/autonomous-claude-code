#!/usr/bin/env python3
"""
Supervisor Loop PoC — Claude Code Session Management Validation

Validates five hypotheses about driving Claude Code from a Python supervisor:

  1. Launch `claude -p "task" --output-format json` and parse session_id from JSON
  2. Continue a session with `claude --continue -p "follow up" --output-format json`
  3. Observe exit codes on success, error, and (if hit) rate limiting
  4. Capture stdout/stderr in real-time via stream-json
  5. Gracefully terminate a running Claude Code process (SIGTERM then SIGKILL)

Run:
    uv run poc/supervisor_loop.py            # default — all tests
    uv run poc/supervisor_loop.py --test 1   # single test by number
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

LOG_FILE = LOG_DIR / f"supervisor_loop_{int(time.time())}.log"

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)-7s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stderr),
    ],
)
log = logging.getLogger("supervisor_poc")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CLAUDE_BIN = "claude"  # assumed on PATH

# Base flags shared by every invocation
BASE_FLAGS: list[str] = [
    "--dangerously-skip-permissions",
    "--model", "opus",
]

PROCESS_TIMEOUT_S = 120  # per-invocation hard timeout


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

class TestResult(Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    SKIP = "SKIP"


@dataclass
class ClaudeResult:
    """Parsed output from a `claude -p ... --output-format json` call."""
    exit_code: int = -1
    session_id: str = ""
    result_text: str = ""
    is_error: bool = False
    subtype: str = ""
    cost_usd: float = 0.0
    duration_ms: int = 0
    num_turns: int = 0
    raw_stdout: str = ""
    raw_stderr: str = ""
    raw_json: dict[str, Any] = field(default_factory=dict)


@dataclass
class StreamEvent:
    """A single event from stream-json output."""
    event_type: str = ""
    subtype: str = ""
    data: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------

def build_cmd(
    prompt: str,
    *,
    output_format: str = "json",
    extra_flags: list[str] | None = None,
) -> list[str]:
    """Build a claude CLI command list."""
    cmd = [CLAUDE_BIN, "-p", prompt, "--output-format", output_format] + BASE_FLAGS
    if extra_flags:
        cmd.extend(extra_flags)
    return cmd


def parse_json_output(stdout: str, exit_code: int) -> ClaudeResult:
    """Parse the JSON blob returned by `--output-format json`."""
    result = ClaudeResult(exit_code=exit_code, raw_stdout=stdout)
    # stdout may contain non-JSON preamble; find the last JSON object
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        # Try to find last JSON object in output
        last_brace = stdout.rfind("}")
        if last_brace == -1:
            log.error("No JSON found in stdout")
            return result
        first_brace = stdout.rfind("{", 0, last_brace)
        if first_brace == -1:
            log.error("Malformed JSON in stdout")
            return result
        try:
            data = json.loads(stdout[first_brace : last_brace + 1])
        except json.JSONDecodeError:
            log.error("Failed to parse JSON substring from stdout")
            return result

    result.raw_json = data
    result.session_id = data.get("session_id", "")
    result.result_text = data.get("result", "")
    result.is_error = data.get("is_error", False)
    result.subtype = data.get("subtype", "")
    result.cost_usd = data.get("total_cost_usd", 0.0)
    result.duration_ms = data.get("duration_ms", 0)
    result.num_turns = data.get("num_turns", 0)
    return result


async def run_claude(
    prompt: str,
    *,
    output_format: str = "json",
    extra_flags: list[str] | None = None,
    timeout: float = PROCESS_TIMEOUT_S,
    cwd: str | Path | None = None,
) -> ClaudeResult:
    """
    Launch `claude -p <prompt>` asynchronously. Returns parsed ClaudeResult.
    Captures both stdout and stderr.
    """
    cmd = build_cmd(prompt, output_format=output_format, extra_flags=extra_flags)
    cmd_str = " ".join(cmd)
    log.info(">>> %s", cmd_str)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
    except asyncio.TimeoutError:
        log.warning("Process timed out after %.0fs — terminating", timeout)
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            log.warning("SIGTERM did not work — sending SIGKILL")
            proc.kill()
            await proc.wait()
        return ClaudeResult(
            exit_code=-1,
            raw_stderr="TIMEOUT",
        )

    stdout_str = stdout_bytes.decode("utf-8", errors="replace")
    stderr_str = stderr_bytes.decode("utf-8", errors="replace")
    exit_code = proc.returncode or 0

    log.info("<<< exit_code=%d  stdout=%d bytes  stderr=%d bytes",
             exit_code, len(stdout_str), len(stderr_str))
    if stderr_str.strip():
        log.debug("STDERR: %s", stderr_str[:500])

    result = parse_json_output(stdout_str, exit_code)
    result.raw_stderr = stderr_str
    return result


# ---------------------------------------------------------------------------
# Test 1: Launch and parse session_id
# ---------------------------------------------------------------------------

async def test_1_launch_and_parse() -> tuple[TestResult, ClaudeResult]:
    """
    Hypothesis: We can launch `claude -p "Say hello" --output-format json`
    and parse session_id, result text, cost, and duration from the JSON output.
    """
    log.info("=" * 60)
    log.info("TEST 1: Launch claude -p and parse JSON output")
    log.info("=" * 60)

    result = await run_claude("Say hello in one short sentence.")

    log.info("  session_id : %s", result.session_id)
    log.info("  result_text: %s", result.result_text[:200] if result.result_text else "(empty)")
    log.info("  is_error   : %s", result.is_error)
    log.info("  subtype    : %s", result.subtype)
    log.info("  cost_usd   : %.6f", result.cost_usd)
    log.info("  duration_ms: %d", result.duration_ms)
    log.info("  num_turns  : %d", result.num_turns)
    log.info("  exit_code  : %d", result.exit_code)

    if result.session_id and result.result_text and not result.is_error:
        log.info("TEST 1: PASS")
        return TestResult.PASS, result
    else:
        log.error("TEST 1: FAIL — missing session_id or result, or is_error=true")
        return TestResult.FAIL, result


# ---------------------------------------------------------------------------
# Test 2: Session continuation with --continue
# ---------------------------------------------------------------------------

async def test_2_session_continue(prior: ClaudeResult) -> tuple[TestResult, ClaudeResult]:
    """
    Hypothesis: We can continue the session with
    `claude --continue -p "What did you just say?" --output-format json`
    and the model remembers the prior exchange.
    """
    log.info("=" * 60)
    log.info("TEST 2: Continue session with --continue")
    log.info("=" * 60)

    if not prior.session_id:
        log.warning("No prior session_id — skipping test 2")
        return TestResult.SKIP, ClaudeResult()

    # Use --resume with the session_id for deterministic continuation
    result = await run_claude(
        "What did you just say? Repeat it exactly.",
        extra_flags=["--resume", prior.session_id],
    )

    log.info("  session_id : %s", result.session_id)
    log.info("  result_text: %s", result.result_text[:300] if result.result_text else "(empty)")
    log.info("  is_error   : %s", result.is_error)
    log.info("  cost_usd   : %.6f", result.cost_usd)

    same_session = result.session_id == prior.session_id
    log.info("  same_session: %s (prior=%s)", same_session, prior.session_id)

    if result.result_text and not result.is_error:
        log.info("TEST 2: PASS")
        return TestResult.PASS, result
    else:
        log.error("TEST 2: FAIL")
        return TestResult.FAIL, result


# ---------------------------------------------------------------------------
# Test 3: Exit code observation
# ---------------------------------------------------------------------------

async def test_3_exit_codes() -> tuple[TestResult, dict[str, int]]:
    """
    Hypothesis: exit code 0 on success.
    We test success and an intentional error (invalid flag).
    Rate-limit exit codes are observed opportunistically.
    """
    log.info("=" * 60)
    log.info("TEST 3: Exit code observation")
    log.info("=" * 60)

    codes: dict[str, int] = {}

    # 3a: Success case
    result_ok = await run_claude("Say OK.")
    codes["success"] = result_ok.exit_code
    log.info("  success exit_code: %d", result_ok.exit_code)

    # 3b: Error case — use a very low max-turns to force an early stop
    # which returns is_error=true when the turn limit is hit
    result_err = await run_claude(
        "Write a 10-step plan for building a house, with detailed sub-steps for each.",
        extra_flags=["--max-turns", "1"],
    )
    codes["max_turns_exceeded"] = result_err.exit_code
    log.info("  max_turns_exceeded exit_code: %d, is_error: %s, subtype: %s",
             result_err.exit_code, result_err.is_error, result_err.subtype)

    # 3c: Invalid invocation (bad flag) — should get non-zero exit
    cmd = [CLAUDE_BIN, "-p", "test", "--nonexistent-flag-xyz"]
    log.info(">>> %s", " ".join(cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=30)
    codes["invalid_flag"] = proc.returncode or 0
    log.info("  invalid_flag exit_code: %d, stderr: %s",
             codes["invalid_flag"], stderr_bytes.decode()[:200])

    log.info("  Collected exit codes: %s", codes)

    # Success criteria: success=0, invalid_flag!=0
    if codes["success"] == 0 and codes["invalid_flag"] != 0:
        log.info("TEST 3: PASS")
        return TestResult.PASS, codes
    else:
        log.error("TEST 3: FAIL — unexpected exit codes")
        return TestResult.FAIL, codes


# ---------------------------------------------------------------------------
# Test 4: Real-time stream capture with stream-json
# ---------------------------------------------------------------------------

async def test_4_realtime_stream() -> tuple[TestResult, list[StreamEvent]]:
    """
    Hypothesis: Using `--output-format stream-json` we can capture
    newline-delimited JSON events in real-time while the process runs.
    """
    log.info("=" * 60)
    log.info("TEST 4: Real-time stream capture (stream-json)")
    log.info("=" * 60)

    cmd = build_cmd(
        "Count from 1 to 3, saying each number on its own line.",
        output_format="stream-json",
    )
    log.info(">>> %s", " ".join(cmd))

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    events: list[StreamEvent] = []
    assert proc.stdout is not None

    try:
        while True:
            line = await asyncio.wait_for(
                proc.stdout.readline(), timeout=PROCESS_TIMEOUT_S
            )
            if not line:
                break
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue

            log.debug("  STREAM> %s", text[:300])

            try:
                data = json.loads(text)
                evt = StreamEvent(
                    event_type=data.get("type", ""),
                    subtype=data.get("subtype", ""),
                    data=data,
                )
                events.append(evt)
            except json.JSONDecodeError:
                log.debug("  (non-JSON line skipped)")

    except asyncio.TimeoutError:
        log.warning("Stream read timed out")
        proc.terminate()

    await proc.wait()
    exit_code = proc.returncode or 0

    # Summarize captured events
    event_types = [f"{e.event_type}/{e.subtype}" if e.subtype else e.event_type for e in events]
    log.info("  Captured %d events: %s", len(events), event_types)
    log.info("  exit_code: %d", exit_code)

    # Check for system/init event with session_id
    init_events = [e for e in events if e.event_type == "system" and e.subtype == "init"]
    result_events = [e for e in events if e.event_type == "result"]
    has_init = len(init_events) > 0
    has_result = len(result_events) > 0

    log.info("  has system/init: %s", has_init)
    log.info("  has result: %s", has_result)

    if init_events:
        log.info("  init session_id: %s", init_events[0].data.get("session_id", ""))

    if events and has_result:
        log.info("TEST 4: PASS")
        return TestResult.PASS, events
    else:
        log.error("TEST 4: FAIL — no events or missing result event")
        return TestResult.FAIL, events


# ---------------------------------------------------------------------------
# Test 5: Graceful termination
# ---------------------------------------------------------------------------

async def test_5_graceful_termination() -> tuple[TestResult, dict[str, Any]]:
    """
    Hypothesis: We can SIGTERM a running claude process and it terminates
    cleanly. If SIGTERM doesn't work within a grace period, SIGKILL works.
    """
    log.info("=" * 60)
    log.info("TEST 5: Graceful termination (SIGTERM / SIGKILL)")
    log.info("=" * 60)

    # Give it a long task so it's definitely still running when we terminate
    cmd = build_cmd(
        "Write a very detailed 2000-word essay about the history of computing.",
        output_format="stream-json",
    )
    log.info(">>> %s", " ".join(cmd))

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    info: dict[str, Any] = {"pid": proc.pid}

    # Wait a few seconds for it to start producing output
    log.info("  Waiting 5s for process to start (pid=%d)...", proc.pid)
    await asyncio.sleep(5)

    # Check it's still running
    if proc.returncode is not None:
        log.warning("  Process already exited before termination test")
        info["already_exited"] = True
        info["exit_code"] = proc.returncode
        log.info("TEST 5: SKIP — process exited too fast")
        return TestResult.SKIP, info

    # Phase 1: SIGTERM
    log.info("  Sending SIGTERM to pid %d...", proc.pid)
    t0 = time.monotonic()
    proc.terminate()

    try:
        await asyncio.wait_for(proc.wait(), timeout=15)
        elapsed = time.monotonic() - t0
        info["sigterm_worked"] = True
        info["sigterm_elapsed_s"] = round(elapsed, 2)
        info["exit_code_after_sigterm"] = proc.returncode
        log.info("  SIGTERM succeeded in %.2fs, exit_code=%s",
                 elapsed, proc.returncode)
    except asyncio.TimeoutError:
        log.warning("  SIGTERM did not terminate within 15s — sending SIGKILL")
        info["sigterm_worked"] = False
        proc.kill()
        await proc.wait()
        elapsed = time.monotonic() - t0
        info["sigkill_elapsed_s"] = round(elapsed, 2)
        info["exit_code_after_sigkill"] = proc.returncode
        log.info("  SIGKILL succeeded in %.2fs, exit_code=%s",
                 elapsed, proc.returncode)

    log.info("  Termination info: %s", info)

    if info.get("sigterm_worked") or info.get("exit_code_after_sigkill") is not None:
        log.info("TEST 5: PASS")
        return TestResult.PASS, info
    else:
        log.error("TEST 5: FAIL — could not terminate process")
        return TestResult.FAIL, info


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run_all_tests(selected: int | None = None) -> dict[int, TestResult]:
    """Run all (or one) PoC tests and return results."""
    results: dict[int, TestResult] = {}

    log.info("=" * 60)
    log.info("SUPERVISOR LOOP PoC — Starting")
    log.info("Log file: %s", LOG_FILE)
    log.info("=" * 60)

    # Test 1
    if selected is None or selected == 1:
        t1_result, t1_data = await test_1_launch_and_parse()
        results[1] = t1_result
    else:
        t1_data = ClaudeResult()

    # Test 2 (depends on test 1's session_id)
    if selected is None or selected == 2:
        if t1_data.session_id:
            t2_result, _ = await test_2_session_continue(t1_data)
        else:
            # Run test 1 first to get a session_id
            log.info("Running test 1 first to obtain session_id for test 2...")
            _, t1_data = await test_1_launch_and_parse()
            t2_result, _ = await test_2_session_continue(t1_data)
        results[2] = t2_result

    # Test 3
    if selected is None or selected == 3:
        t3_result, _ = await test_3_exit_codes()
        results[3] = t3_result

    # Test 4
    if selected is None or selected == 4:
        t4_result, _ = await test_4_realtime_stream()
        results[4] = t4_result

    # Test 5
    if selected is None or selected == 5:
        t5_result, _ = await test_5_graceful_termination()
        results[5] = t5_result

    # Summary
    log.info("")
    log.info("=" * 60)
    log.info("SUMMARY")
    log.info("=" * 60)
    for num, res in sorted(results.items()):
        log.info("  Test %d: %s", num, res.value)
    log.info("Log file: %s", LOG_FILE)
    log.info("=" * 60)

    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Supervisor Loop PoC")
    parser.add_argument(
        "--test", type=int, choices=[1, 2, 3, 4, 5], default=None,
        help="Run a single test by number (default: all)",
    )
    args = parser.parse_args()

    results = asyncio.run(run_all_tests(selected=args.test))

    # Exit non-zero if any test failed
    if any(r == TestResult.FAIL for r in results.values()):
        sys.exit(1)


if __name__ == "__main__":
    main()
