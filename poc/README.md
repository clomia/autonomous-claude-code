# PoC: Supervisor Loop

Validates that a Python supervisor process can drive Claude Code sessions end-to-end.

## What each test validates

| Test | Hypothesis | How |
|------|-----------|-----|
| **1** | Can we launch `claude -p` and parse structured output? | Runs `claude -p "Say hello" --output-format json`, parses `session_id`, `result`, `total_cost_usd`, `duration_ms` from JSON stdout. |
| **2** | Can we continue a session? | Uses `--resume <session_id>` from Test 1 to send a follow-up prompt and checks the model has context of the prior exchange. |
| **3** | What exit codes does Claude Code return? | Collects exit codes for: success (expect 0), max-turns exceeded, and invalid CLI flag (expect non-zero). Rate-limit codes are documented but require hitting the actual limit. |
| **4** | Can we capture stdout/stderr in real-time? | Uses `--output-format stream-json` and reads newline-delimited JSON events as they arrive (`system/init`, `assistant`, `result`, etc.). |
| **5** | Can we gracefully terminate a running process? | Starts a long task, waits for output, sends `SIGTERM`, verifies shutdown within 15 s. Falls back to `SIGKILL` if needed. |

## How to run

```bash
# All tests (takes ~2-3 minutes depending on API latency)
uv run poc/supervisor_loop.py

# Single test
uv run poc/supervisor_loop.py --test 1
uv run poc/supervisor_loop.py --test 4
```

## Prerequisites

- `claude` CLI on PATH, logged in (Claude Max subscription)
- Python 3.12+ (no third-party dependencies -- stdlib only)

## Logs

Each run writes a timestamped log to `poc/logs/`. The log file path is printed at the end of every run.

## Exit code mapping (from Claude Code docs)

| Claude exit code | Meaning |
|-----------------|---------|
| 0 | Success |
| 1 | General error / is_error=true in JSON |
| 2 | Hook block (used by Stop hooks) |

Rate limiting is surfaced via `stream-json` events (`system/api_retry` with `error: "rate_limit"`), not via process exit codes -- Claude Code retries internally.

## Key findings to look for

After running, check the log file for:

1. **session_id format** -- UUID that can be fed back to `--resume`
2. **JSON structure** -- fields available in the result object
3. **stream-json event sequence** -- what events fire and in what order
4. **SIGTERM behavior** -- how quickly Claude Code shuts down and what exit code it uses
5. **Cost tracking** -- `total_cost_usd` accuracy for budget management
