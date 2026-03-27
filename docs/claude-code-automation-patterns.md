# Claude Code Automation Patterns for Perpetual Autonomous Agents

> Created: 2026-03-25
> Scope: Practical reference for running Claude Code in headless/automated mode to build a perpetual autonomous agent. Covers session management, permissions, model configuration, hooks, CLAUDE.md, and real-world automation patterns.

---

## 1. Session Continuation

### 1.1 `--continue` Flag

`--continue` (short: `-c`) loads the **most recent conversation in the current directory**. It resumes the same session ID, appending new messages to the existing conversation. Full conversation history is restored, but **session-scoped permissions are NOT inherited** -- you must re-approve those.

```bash
# First request
claude -p "Review this codebase for performance issues"

# Continue the most recent conversation (same session)
claude -p "Now focus on the database queries" --continue
claude -p "Generate a summary of all issues found" --continue
```

Key behaviors:
- Sessions are **directory-scoped** -- `--continue` picks the most recent session from the current working directory
- You CAN chain multiple `--continue` calls to build context across invocations
- Each `--continue` call appends to the same session file
- Works in both interactive (`claude -c`) and non-interactive (`claude -c -p "query"`) modes

### 1.2 `--resume` with Session ID

`--resume` (short: `-r`) resumes a **specific** session by ID or name. This is the mechanism for multi-session harnesses.

```bash
# Capture session ID from first call
session_id=$(claude -p "Start a review" --output-format json | jq -r '.session_id')

# Resume that specific session
claude -p "Continue that review" --resume "$session_id"
```

You can also resume by **name** if you used `--name`:

```bash
claude -p "Start auth refactor" --name "auth-refactor" --output-format json
claude -r "auth-refactor" -p "Continue the refactor"
```

### 1.3 `--session-id` for Deterministic IDs

`--session-id` lets you specify a UUID upfront rather than letting Claude generate one:

```bash
claude --session-id "550e8400-e29b-41d4-a716-446655440000" -p "Start work"
```

This is useful for automation where you want predictable session identifiers.

### 1.4 `--fork-session` for Branching

When resuming, `--fork-session` creates a **new session ID** while preserving conversation history up to that point. The original session remains unchanged.

```bash
claude --continue --fork-session -p "Try a different approach"
```

### 1.5 `--output-format json` Response Fields

```json
{
  "type": "result",
  "subtype": "success",
  "result": "The actual response text",
  "session_id": "uuid-here",
  "total_cost_usd": 0.001234,
  "is_error": false,
  "num_turns": 5,
  "duration_ms": 2500,
  "duration_api_ms": 2100,
  "usage": {
    "input_tokens": 10000,
    "output_tokens": 500,
    "cache_creation_input_tokens": 5000,
    "cache_read_input_tokens": 3000
  },
  "modelUsage": {
    "claude-opus-4-6": {
      "inputTokens": 10000,
      "outputTokens": 500,
      "cacheReadInputTokens": 3000,
      "costUSD": 0.001234
    }
  }
}
```

Error response:
```json
{
  "type": "result",
  "subtype": "error",
  "is_error": true,
  "result": "Error message text",
  "session_id": "uuid-here"
}
```

### 1.6 `--output-format stream-json` Events

Newline-delimited JSON events. Key event types:

| Event | Description |
|-------|-------------|
| `system/init` | Session initialized, contains `session_id` |
| `assistant` | Claude's response messages |
| `tool_use` | Tool call initiated |
| `tool_result` | Tool call completed |
| `system/api_retry` | API retry event with `attempt`, `max_retries`, `retry_delay_ms`, `error_status`, `error` category |

The `api_retry` event is critical for automation -- it lets you detect rate limiting:

| `error` field | Meaning |
|---------------|---------|
| `rate_limit` | Rate limited, will retry |
| `server_error` | Server error, will retry |
| `authentication_failed` | Auth failure |
| `billing_error` | Billing issue |
| `max_output_tokens` | Output truncated |

### 1.7 Context Window and Compaction with `--continue`

When context fills up during a continued session:
- Claude Code **automatically compacts** -- clears older tool outputs first, then summarizes the conversation
- Your requests and key code snippets are preserved; detailed early instructions may be lost
- **CLAUDE.md fully survives compaction** -- after compaction, Claude re-reads CLAUDE.md from disk and re-injects it fresh
- Auto memory (MEMORY.md) first 200 lines are also reloaded
- Multi-turn sessions increase token consumption by 30-50% per additional turn
- With 1M context (opus[1m]), sessions can hold significantly more before compaction

**Critical insight**: After compaction, Claude loses awareness of files it was working on and needs to re-read them. Put persistent rules in CLAUDE.md, not in conversation history.

### 1.8 `--no-session-persistence`

For one-off calls where you don't need to resume:

```bash
claude -p "Quick question" --no-session-persistence --output-format json
```

---

## 2. Permission Bypassing

### 2.1 Permission Modes (Complete List)

| Mode | Flag | Behavior |
|------|------|----------|
| `default` | `--permission-mode default` | Prompts for file edits and commands |
| `acceptEdits` | `--permission-mode acceptEdits` | Auto-approves file edits, prompts for commands |
| `plan` | `--permission-mode plan` | Read-only: Claude can analyze but not modify |
| `auto` | `--permission-mode auto` | Background classifier reviews actions; blocks risky ones |
| `dontAsk` | `--permission-mode dontAsk` | Auto-denies unless pre-approved via allow rules |
| `bypassPermissions` | `--permission-mode bypassPermissions` | Skips all permission prompts |

### 2.2 `--dangerously-skip-permissions`

Equivalent to `--permission-mode bypassPermissions`. Bypasses ALL permission prompts except writes to `.git`, `.claude`, `.vscode`, `.idea` directories.

```bash
claude -p "Refactor the auth module" --dangerously-skip-permissions
```

What it enables:
- All file reads, writes, and edits without prompting
- All Bash commands without prompting
- All MCP tool calls without prompting
- All web fetches without prompting

What it still protects:
- Writes to `.git/` still prompt (prevents repository corruption)
- Writes to `.claude/`, `.vscode/`, `.idea/` still prompt
- Exception: `.claude/commands/`, `.claude/agents/`, `.claude/skills/` do NOT prompt

### 2.3 `auto` Mode (Safer Alternative)

Auto mode (introduced March 24, 2026) uses a **classifier model** (Sonnet 4.6) to evaluate each action before execution:

```bash
claude --permission-mode auto -p "Fix all lint errors" --enable-auto-mode
```

Key behaviors:
- Blocks: downloading and executing code (curl|bash), data exfiltration, production deploys, mass deletion, force push, granting permissions
- Allows: local file ops in working directory, reading .env, read-only HTTP, pushing to current branch
- **Fallback**: if classifier blocks 3 times consecutively or 20 times total, falls back to prompting
- **In `-p` mode**: aborts if fallback triggers (no user to prompt)
- Requires Team/Enterprise plan + Sonnet 4.6 or Opus 4.6
- Classifier calls count toward token usage

### 2.4 `--allowedTools` (Fine-Grained Permission)

Pre-approve specific tools without bypassing all permissions:

```bash
# Allow all file operations and specific git commands
claude -p "Review and commit" \
  --allowedTools "Bash(git diff *)" "Bash(git log *)" "Bash(git status *)" "Bash(git commit *)" "Read" "Edit"
```

Pattern syntax:
- `Bash` -- matches ALL bash commands
- `Bash(npm run *)` -- matches commands starting with `npm run `
- `Bash(git commit *)` -- the space before `*` enforces word boundary
- `Read` -- matches all file reads
- `Edit` -- matches all file edits
- `WebFetch(domain:example.com)` -- restricts to specific domain
- `mcp__servername__toolname` -- specific MCP tool
- `Agent(my-agent)` -- specific subagent

**For autonomous agent with full access:**

```bash
claude -p "Do the work" \
  --allowedTools "Bash" "Read" "Edit" "Write" "Glob" "Grep" "WebFetch" "WebSearch" "Agent"
```

### 2.5 `--disallowedTools`

Remove tools entirely from Claude's context:

```bash
claude -p "Read-only analysis" --disallowedTools "Edit" "Write" "Bash"
```

### 2.6 `--allow-dangerously-skip-permissions` (Composable)

Adds bypassPermissions to the mode cycle without activating it:

```bash
claude --permission-mode plan --allow-dangerously-skip-permissions
```

### 2.7 `dontAsk` Mode for Locked-Down Automation

Pre-define exactly what's allowed; everything else is silently denied:

```bash
# In .claude/settings.json
{
  "permissions": {
    "defaultMode": "dontAsk",
    "allow": [
      "Read",
      "Glob",
      "Grep",
      "Bash(npm test *)",
      "Bash(git *)",
      "Edit(/src/**)"
    ]
  }
}
```

---

## 3. Model and Effort Control

### 3.1 Setting the Model

Priority order (highest to lowest):
1. `/model <alias>` during session
2. `--model <alias|name>` at startup
3. `ANTHROPIC_MODEL` environment variable
4. `model` field in settings file

```bash
# Using alias (always resolves to latest)
claude --model opus -p "Complex task"
claude --model opus[1m] -p "Long session task"

# Using full model name (pinned version)
claude --model claude-opus-4-6 -p "Task"

# Via environment variable
ANTHROPIC_MODEL=opus[1m] claude -p "Task"

# In settings.json
{ "model": "opus[1m]" }
```

Model aliases:

| Alias | Resolves To | Use Case |
|-------|-------------|----------|
| `default` | Depends on plan (Opus 4.6 for Max/Team) | Auto-selected |
| `sonnet` | Sonnet 4.6 | Daily coding |
| `opus` | Opus 4.6 | Complex reasoning |
| `opus[1m]` | Opus 4.6 with 1M context | Long sessions |
| `sonnet[1m]` | Sonnet 4.6 with 1M context | Long sessions |
| `haiku` | Haiku | Simple tasks |
| `opusplan` | Opus in plan mode, Sonnet in execution | Hybrid approach |

### 3.2 Effort Levels

```bash
# CLI flag (session-scoped, does not persist)
claude --effort max -p "Deep analysis"

# Environment variable (takes precedence over all other methods)
CLAUDE_CODE_EFFORT_LEVEL=max claude -p "Deep analysis"

# In settings.json (persists across sessions)
{ "effortLevel": "high" }

# During session
/effort max
```

Available levels:

| Level | Behavior | Availability |
|-------|----------|-------------|
| `low` | Fastest, minimal thinking | All supported models |
| `medium` | Default for Opus on Max/Team | All supported models |
| `high` | Deep reasoning | All supported models |
| `max` | Deepest reasoning, no token constraint | **Opus 4.6 only**, session-scoped, does not persist |
| `auto` | Model decides | Resets to model default |

**`max` is available on Opus 4.6 only and does not persist** -- you must set it every session via `--effort max` or `CLAUDE_CODE_EFFORT_LEVEL=max`.

### 3.3 Subagent Model Inheritance

By default, subagents use the model controlled by `CLAUDE_CODE_SUBAGENT_MODEL`:

```bash
# Force all subagents to use Opus
export CLAUDE_CODE_SUBAGENT_MODEL=claude-opus-4-6
```

Subagent frontmatter can override model and effort:

```markdown
---
name: deep-reviewer
model: opus
effort: max
---
You are a thorough code reviewer...
```

When the `effort` field is set in skill/subagent frontmatter, it overrides the session level but NOT the `CLAUDE_CODE_EFFORT_LEVEL` environment variable.

### 3.4 Environment Variables for Model Pinning

| Variable | Controls |
|----------|----------|
| `ANTHROPIC_MODEL` | Primary model |
| `ANTHROPIC_DEFAULT_OPUS_MODEL` | Model for `opus` alias |
| `ANTHROPIC_DEFAULT_SONNET_MODEL` | Model for `sonnet` alias |
| `ANTHROPIC_DEFAULT_HAIKU_MODEL` | Model for `haiku` alias |
| `CLAUDE_CODE_SUBAGENT_MODEL` | Model for subagents |
| `CLAUDE_CODE_EFFORT_LEVEL` | Effort level (overrides all) |
| `CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING` | Set to `1` to revert to fixed thinking budget |
| `CLAUDE_CODE_DISABLE_1M_CONTEXT` | Set to `1` to remove 1M variants from picker |

### 3.5 Complete Configuration for Maximum Quality

```bash
export ANTHROPIC_MODEL="opus[1m]"
export CLAUDE_CODE_EFFORT_LEVEL="max"
export CLAUDE_CODE_SUBAGENT_MODEL="claude-opus-4-6"

claude -p "Do the work" \
  --model "opus[1m]" \
  --effort max \
  --dangerously-skip-permissions
```

---

## 4. Hooks for Automation

### 4.1 All Hook Events (24 Total)

| Event | When | Can Block? | Key for Automation |
|-------|------|------------|-------------------|
| `SessionStart` | New/resumed session | No | Inject context |
| `Stop` | Claude finishes responding | **Yes** | **Prevent stopping** |
| `Notification` | Notification sent | No | Forward to Slack |
| `PreToolUse` | Before tool execution | Yes | Modify/block tools |
| `PostToolUse` | After tool succeeds | No | Add context |
| `PostToolUseFailure` | After tool fails | No | Error tracking |
| `UserPromptSubmit` | User submits prompt | Yes | Filter/enhance prompts |
| `PermissionRequest` | Permission dialog | Yes | Auto-approve |
| `SubagentStart` | Subagent spawned | No | Inject context into subagent |
| `SubagentStop` | Subagent finishes | Yes | Prevent subagent stopping |
| `PreCompact` | Before compaction | No | Pre-compaction actions |
| `PostCompact` | After compaction | No | Post-compaction refresh |
| `SessionEnd` | Session terminates | No | Cleanup |
| `StopFailure` | Turn ends due to API error | No | Error detection |
| `InstructionsLoaded` | CLAUDE.md loaded | No | Debug instruction loading |
| `ConfigChange` | Config file changes | Yes | Block config changes |
| `WorktreeCreate` | Worktree created | Yes | Control worktree creation |
| `WorktreeRemove` | Worktree removed | No | Cleanup |
| `TeammateIdle` | Agent teammate goes idle | Yes | Prevent idle |
| `TaskCompleted` | Task marked complete | Yes | Prevent premature completion |
| `Elicitation` | MCP requests input | Yes | Auto-respond |
| `ElicitationResult` | User responds to MCP | Yes | Intercept responses |

### 4.2 Stop Hook: Preventing Claude from Stopping

This is the **core mechanism for perpetual operation**. The Stop hook can block Claude's exit and force continuation.

Hook configuration in `.claude/settings.json`:
```json
{
  "hooks": {
    "Stop": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "/path/to/stop-hook.sh",
            "timeout": 10000
          }
        ]
      }
    ]
  }
}
```

Stop hook input JSON (received via stdin):
```json
{
  "session_id": "abc123",
  "transcript_path": "/Users/.../.claude/projects/.../transcript.jsonl",
  "cwd": "/Users/.../project",
  "permission_mode": "bypassPermissions",
  "hook_event_name": "Stop",
  "stop_hook_active": true,
  "last_assistant_message": "I've completed the refactoring..."
}
```

Stop hook output to **block stopping** (stdout JSON):
```json
{
  "hookSpecificOutput": {
    "hookEventName": "Stop",
    "decision": "block",
    "reason": "Must continue working on remaining tasks. Check TASKS.md for next item."
  }
}
```

Alternative: **exit code 2** also blocks stopping (stderr used as reason):
```bash
#!/bin/bash
echo "Must continue: tasks remaining" >&2
exit 2
```

### 4.3 SessionStart Hook: Injecting Context

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "/path/to/session-start.sh",
            "timeout": 10000
          }
        ]
      }
    ]
  }
}
```

The script can inject context via stdout JSON:
```json
{
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": "Current mission: Implement OAuth. Last session completed: database schema. Next: API endpoints. See MISSION.md for full context."
  }
}
```

The script can also set environment variables:
```bash
#!/bin/bash
if [ -n "$CLAUDE_ENV_FILE" ]; then
  echo 'export NODE_ENV=production' >> "$CLAUDE_ENV_FILE"
  echo 'export DEBUG_LOG=true' >> "$CLAUDE_ENV_FILE"
fi

# Inject context via JSON output
cat <<'EOF'
{
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": "Session started. Read MISSION.md for current objectives."
  }
}
EOF
exit 0
```

### 4.4 Notification Hook: Forwarding to Slack

```json
{
  "hooks": {
    "Notification": [
      {
        "matcher": "permission_prompt",
        "hooks": [
          {
            "type": "command",
            "command": "curl -X POST -H 'Content-Type: application/json' -d '{\"text\":\"Claude needs permission\"}' https://hooks.slack.com/services/YOUR/WEBHOOK/URL",
            "timeout": 5000
          }
        ]
      }
    ]
  }
}
```

Notification input:
```json
{
  "session_id": "abc123",
  "hook_event_name": "Notification",
  "message": "Claude needs your permission to use Bash",
  "title": "Permission needed",
  "notification_type": "permission_prompt"
}
```

Matcher values for Notification:
- `permission_prompt` -- Permission dialog
- `idle_prompt` -- Claude is idle
- `auth_success` -- Authentication succeeded
- `elicitation_dialog` -- MCP elicitation dialog

### 4.5 Context-Adding Hooks Summary

Multiple hooks can inject `additionalContext` that Claude sees:

| Hook Event | Can Add Context | Can Modify Input |
|------------|----------------|-----------------|
| `SessionStart` | Yes, via `additionalContext` | No |
| `UserPromptSubmit` | Yes, via `additionalContext` | No (can block) |
| `PreToolUse` | Yes, via `additionalContext` | Yes, via `updatedInput` |
| `PostToolUse` | Yes, via `additionalContext` | No |
| `PostToolUseFailure` | Yes, via `additionalContext` | No |
| `SubagentStart` | Yes, via `additionalContext` | No |

### 4.6 Exit Code Reference

| Code | Meaning | Effect |
|------|---------|--------|
| **0** | Success | Parse JSON output; proceed |
| **2** | Block | Ignore stdout; use stderr as reason; block action |
| **Other** | Non-blocking error | Show stderr in verbose; continue |

### 4.7 Hook Configuration Locations

| Location | Scope | Shareable |
|----------|-------|-----------|
| `~/.claude/settings.json` | All projects | No (personal) |
| `.claude/settings.json` | Single project | Yes (commit to repo) |
| `.claude/settings.local.json` | Single project | No (gitignored) |
| Managed policy settings | Organization-wide | Admin-controlled |
| Plugin `hooks/hooks.json` | When plugin enabled | Yes |
| Skill/agent frontmatter | While active | Yes |

---

## 5. CLAUDE.md for Persistent Instructions

### 5.1 Location Hierarchy

| Scope | Location | Purpose |
|-------|----------|---------|
| Managed policy | `/Library/Application Support/ClaudeCode/CLAUDE.md` (macOS) | Organization-wide, cannot be excluded |
| Project | `./CLAUDE.md` or `./.claude/CLAUDE.md` | Shared via git |
| User | `~/.claude/CLAUDE.md` | Personal, all projects |
| Parent dirs | `../CLAUDE.md` | Monorepo parent |
| Child dirs | `./subdir/CLAUDE.md` | Loaded on demand when Claude reads files there |

**Resolution order**: Claude walks UP the directory tree from cwd, loading each CLAUDE.md. Child directory CLAUDE.md files load on demand.

### 5.2 Recommended Size

**Target under 200 lines per CLAUDE.md file.** Longer files consume more context and reduce adherence. The official docs are explicit: "If Claude keeps doing something you don't want despite having a rule against it, the file is probably too long and the rule is getting lost."

### 5.3 @import Syntax

CLAUDE.md supports importing additional files:

```markdown
See @README.md for project overview and @package.json for available npm commands.

# Additional Instructions
- Git workflow: @docs/git-instructions.md
- Personal overrides: @~/.claude/my-project-instructions.md
```

Rules:
- Relative paths resolve relative to the file containing the import
- Absolute paths and `~/` paths work
- Maximum import depth: **5 hops** (recursive imports)
- First encounter of external imports shows an approval dialog

### 5.4 Compaction Behavior

**CLAUDE.md fully survives compaction.** After `/compact` or auto-compaction, Claude re-reads CLAUDE.md from disk and re-injects it fresh into the session. This is the single most important fact for autonomous agents -- CLAUDE.md is your reliable anchor across compaction events.

What survives compaction:
- CLAUDE.md content (re-read from disk)
- Auto memory (MEMORY.md first 200 lines)
- Key code snippets and recent requests

What may be lost:
- Detailed instructions given only in conversation
- Older tool outputs
- Early conversation context

### 5.5 CLAUDE.md Structure for Autonomous Agent

```markdown
# Purpose
[Single paragraph: the agent's enduring purpose/direction]

# Current Mission
Read MISSION.md for current mission details and status.

# System Architecture
- Supervisor: external process that restarts sessions
- State files: all state in ./state/ directory (git-tracked)
- Mission queue: ./state/missions.json

# Compact Instructions
When compacting, ALWAYS preserve:
- Current mission ID and status
- List of modified files this session
- Any blockers or pending owner requests
- Contents of MISSION.md reference

# Critical Rules
- IMPORTANT: Always read MISSION.md before taking any action
- IMPORTANT: Always update ./state/session-log.json after completing work
- IMPORTANT: Never modify files in ./state/ without reading them first
- Use Korean for all Slack messages to the owner

# Workflow
1. Read MISSION.md and ./state/missions.json
2. Pick the highest-priority non-blocked mission
3. Execute the mission (explore, plan, implement, verify)
4. Update mission status in ./state/missions.json
5. Write session notes to ./state/session-notes.md
6. If blocked, create owner request via Slack hook

# Code Standards
[project-specific rules]

# Tools & Commands
[build, test, deploy commands]
```

### 5.6 `.claude/rules/` for Modular Instructions

For larger instruction sets, use path-scoped rules:

```
.claude/
├── CLAUDE.md            # Main instructions
└── rules/
    ├── code-style.md    # Always loaded
    ├── testing.md       # Always loaded
    └── api/
        └── conventions.md  # Loaded when working with API files
```

Path-scoped example:
```markdown
---
paths:
  - "src/api/**/*.ts"
---
# API Rules
- Use kebab-case for URL paths
- Always include pagination for list endpoints
```

---

## 6. Practical Automation Patterns

### 6.1 The Ralph Loop (Official Anthropic Plugin)

The Ralph Wiggum plugin is now an **official Anthropic plugin** in the claude-code repository. It uses a **Stop hook** to prevent Claude from exiting and re-injects the prompt.

Core mechanism:
1. User invokes `/ralph-loop "task" --completion-promise "DONE" --max-iterations 50`
2. Claude works on the task
3. When Claude tries to stop, the Stop hook intercepts
4. Hook checks if the completion promise string is in Claude's output
5. If NOT found: blocks stopping, re-feeds the prompt
6. If found: allows normal exit

The key innovation is that this operates **inside the session** (no external bash loop needed for the inner loop). The Stop hook is the forcing function.

**When to use**: well-defined tasks with clear completion criteria, auto-verifiable work (tests pass, lint clean).

### 6.2 Continuous Claude (PR Workflow)

Anand Chowdhary's [continuous-claude](https://github.com/AnandChowdhary/continuous-claude) is a 2,300-line bash script that orchestrates iterative Claude Code runs with full PR lifecycle management.

Core architecture:
```bash
# Simplified core loop
while [ $i -lt $MAX_RUNS ] && [ $total_cost -lt $MAX_COST ]; do
  # Each iteration is a FRESH claude call (no --continue)
  claude -p "$prompt" --dangerously-skip-permissions --output-format stream-json

  # Context passed between iterations via SHARED_TASK_NOTES.md
  # Claude reads this file each iteration for continuity

  # PR workflow
  git checkout -b "iteration-${i}"
  claude -p "$COMMIT_PROMPT" --allowedTools "Bash(git commit *)"
  git push origin HEAD
  gh pr create --title "..." --body "..."

  # Wait for CI checks (30-minute timeout, 10s polling)
  wait_for_pr_checks

  # Merge or close based on CI result
  if checks_pass; then
    gh pr merge --squash
  else
    if $CI_RETRY_ENABLED; then
      attempt_ci_fix_and_recheck
    else
      gh pr close
    fi
  fi

  i=$((i + 1))
done
```

Key configuration:

| Variable | Purpose | Default |
|----------|---------|---------|
| `PROMPT` | Task description | Required |
| `MAX_RUNS` | Iteration limit | Required |
| `MAX_COST` | Dollar cost limit | No limit |
| `MAX_DURATION` | Time limit (e.g., "2h30m") | No limit |
| `COMPLETION_SIGNAL` | Phrase indicating done | None |
| `COMPLETION_THRESHOLD` | Consecutive signals to stop | 3 |
| `NOTES_FILE` | Context file | `SHARED_TASK_NOTES.md` |
| `MERGE_STRATEGY` | squash/merge/rebase | squash |
| `CI_RETRY_ENABLED` | Auto-fix CI failures | true |
| `WORKTREE_NAME` | Parallel execution via git worktrees | None |

Critical prompt instruction for context continuity:
> "This is part of a continuous development loop. You don't need to complete the entire goal in one iteration, just make meaningful progress on one thing, then leave clear notes for the next iteration. Think of it as a relay race where you're passing the baton."

### 6.3 Agent SDK Harness (Python/TypeScript)

The official Claude Agent SDK provides the **programmatic** approach for multi-session orchestration:

```python
import asyncio
from claude_agent_sdk import query, ClaudeAgentOptions

async def supervisor_loop():
    session_id = None

    while True:
        # Read mission state
        mission = read_mission_file()
        if not mission:
            break

        prompt = f"Continue working on: {mission.description}. Read MISSION.md for context."

        options = ClaudeAgentOptions(
            allowed_tools=["Read", "Edit", "Write", "Bash", "Glob", "Grep", "Agent"],
            permission_mode="bypassPermissions",
        )

        # Resume previous session or start new
        if session_id:
            options.resume = session_id

        async for message in query(prompt=prompt, options=options):
            if hasattr(message, "subtype") and message.subtype == "init":
                session_id = message.session_id
            if hasattr(message, "result"):
                process_result(message)

        # Check for rate limiting, errors, completion
        await asyncio.sleep(5)

asyncio.run(supervisor_loop())
```

TypeScript equivalent:
```typescript
import { query } from "@anthropic-ai/claude-agent-sdk";

let sessionId: string | undefined;

for await (const message of query({
  prompt: "Continue working. Read MISSION.md.",
  options: {
    resume: sessionId,
    allowedTools: ["Read", "Edit", "Write", "Bash", "Glob", "Grep", "Agent"],
    permissionMode: "bypassPermissions",
    hooks: {
      Stop: [{ matcher: "*", hooks: [stopHookCallback] }],
      SessionStart: [{ matcher: "*", hooks: [contextInjector] }]
    },
    agents: {
      "deep-reviewer": {
        description: "Reviews code thoroughly",
        prompt: "You are a security-focused code reviewer.",
        tools: ["Read", "Glob", "Grep"]
      }
    }
  }
})) {
  if (message.type === "system" && message.subtype === "init") {
    sessionId = message.session_id;
  }
}
```

### 6.4 Minimal Supervisor Script (Bash)

The simplest perpetual agent pattern:

```bash
#!/bin/bash
# supervisor.sh -- perpetual Claude Code supervisor

export ANTHROPIC_MODEL="opus[1m]"
export CLAUDE_CODE_EFFORT_LEVEL="max"
export CLAUDE_CODE_SUBAGENT_MODEL="claude-opus-4-6"

MAX_RETRIES=3
RETRY_COUNT=0
SESSION_ID=""

while true; do
  echo "[$(date)] Starting new session..."

  # Build resume flag
  RESUME_FLAG=""
  if [ -n "$SESSION_ID" ]; then
    RESUME_FLAG="--resume $SESSION_ID"
  fi

  # Run Claude
  OUTPUT=$(claude -p "Read MISSION.md and continue working on the current mission. Update state files when done." \
    --model "opus[1m]" \
    --effort max \
    --dangerously-skip-permissions \
    --output-format json \
    --max-turns 200 \
    $RESUME_FLAG \
    2>&1)

  # Parse result
  EXIT_CODE=$?
  IS_ERROR=$(echo "$OUTPUT" | jq -r '.is_error // false')
  NEW_SESSION_ID=$(echo "$OUTPUT" | jq -r '.session_id // empty')
  COST=$(echo "$OUTPUT" | jq -r '.total_cost_usd // 0')

  if [ -n "$NEW_SESSION_ID" ]; then
    SESSION_ID="$NEW_SESSION_ID"
  fi

  echo "[$(date)] Session completed. Cost: \$$COST, Error: $IS_ERROR"

  # Handle errors
  if [ "$IS_ERROR" = "true" ] || [ $EXIT_CODE -ne 0 ]; then
    RETRY_COUNT=$((RETRY_COUNT + 1))
    if [ $RETRY_COUNT -ge $MAX_RETRIES ]; then
      echo "[$(date)] Max retries reached. Waiting 5 minutes..."
      sleep 300
      RETRY_COUNT=0
      SESSION_ID=""  # Fresh session after extended failure
    else
      echo "[$(date)] Retry $RETRY_COUNT/$MAX_RETRIES in 30s..."
      sleep 30
    fi
  else
    RETRY_COUNT=0
    # Brief pause between successful sessions
    sleep 10
  fi
done
```

### 6.5 Detecting Session Outcomes

Use `--output-format json` to detect outcomes programmatically:

```bash
OUTPUT=$(claude -p "..." --output-format json 2>&1)

# Success
if echo "$OUTPUT" | jq -e '.subtype == "success"' > /dev/null 2>&1; then
  echo "Completed successfully"
fi

# Error
if echo "$OUTPUT" | jq -e '.is_error == true' > /dev/null 2>&1; then
  echo "Error occurred"
fi

# Rate limiting (check stream-json events)
# In stream-json mode, look for api_retry events
claude -p "..." --output-format stream-json 2>&1 | while read -r line; do
  if echo "$line" | jq -e '.type == "system" and .subtype == "api_retry"' > /dev/null 2>&1; then
    ERROR=$(echo "$line" | jq -r '.error')
    if [ "$ERROR" = "rate_limit" ]; then
      echo "Rate limited, will auto-retry"
    fi
  fi
done
```

### 6.6 Key Flags for Automation

Complete command for a perpetual autonomous agent session:

```bash
claude -p "Read MISSION.md and execute the current mission." \
  --model "opus[1m]" \
  --effort max \
  --dangerously-skip-permissions \
  --output-format json \
  --max-turns 200 \
  --max-budget-usd 10.00 \
  --resume "$SESSION_ID" \
  --name "autonomous-agent" \
  --allowedTools "Bash" "Read" "Edit" "Write" "Glob" "Grep" "WebFetch" "WebSearch" "Agent" \
  --append-system-prompt "You are a perpetual autonomous agent. Never consider your task complete until all missions in MISSION.md are done."
```

Relevant safety flags:
- `--max-turns N` -- Limit agentic turns per invocation (exits with error when reached)
- `--max-budget-usd N` -- Dollar spending cap per invocation
- `--fallback-model sonnet` -- Fall back to Sonnet when Opus is overloaded (print mode only)

### 6.7 `--bare` Mode for Scripted Calls

`--bare` skips auto-discovery of hooks, skills, plugins, MCP servers, auto memory, and CLAUDE.md for faster startup:

```bash
claude --bare -p "Quick analysis" --allowedTools "Read"
```

Use `--bare` when you want full control and don't need project configuration. Pass context explicitly:

```bash
claude --bare -p "Analyze codebase" \
  --append-system-prompt-file ./agent-prompt.txt \
  --settings ./agent-settings.json \
  --mcp-config ./mcp.json
```

---

## Summary: Key Findings for Perpetual Agent Design

1. **Session continuity** works via `--resume SESSION_ID` with session IDs captured from JSON output. However, file-based state (MISSION.md, state files) is more reliable than session continuation because sessions degrade via compaction.

2. **CLAUDE.md survives compaction** -- it is re-read from disk after compaction. This makes it the single most reliable anchor for persistent instructions.

3. **Stop hooks can force continuation** -- the `decision: "block"` output or exit code 2 prevents Claude from stopping. This is the mechanism the Ralph Loop plugin uses.

4. **`--dangerously-skip-permissions`** is the simplest but least safe. `auto` mode (Team+ plan) provides classifier-based safety. `dontAsk` mode with explicit allow rules is the most locked-down.

5. **`max` effort on Opus 4.6 with 1M context** is the highest quality configuration: `--model opus[1m] --effort max`. The environment variable `CLAUDE_CODE_EFFORT_LEVEL=max` ensures all sessions use max effort.

6. **Subagent model is controlled separately** via `CLAUDE_CODE_SUBAGENT_MODEL` or frontmatter `model:` field in agent definitions.

7. **The two dominant automation patterns are**: (a) Stop-hook-based inner loops (Ralph) that keep Claude running within a single session, and (b) External supervisor loops (Continuous Claude) that spawn fresh `claude -p` invocations with file-based context continuity.

8. **The Agent SDK** (Python/TypeScript) provides the most production-ready approach, with native session management, hook callbacks, and structured message objects.

---

Sources:
- [Claude Code Headless/Agent SDK Docs](https://code.claude.com/docs/en/headless)
- [Claude Code Permissions](https://code.claude.com/docs/en/permissions)
- [Claude Code Permission Modes](https://code.claude.com/docs/en/permission-modes)
- [Claude Code Hooks Reference](https://code.claude.com/docs/en/hooks)
- [Claude Code Best Practices](https://code.claude.com/docs/en/best-practices)
- [Claude Code Model Configuration](https://code.claude.com/docs/en/model-config)
- [Claude Code Memory/CLAUDE.md](https://code.claude.com/docs/en/memory)
- [Claude Code How It Works](https://code.claude.com/docs/en/how-claude-code-works)
- [Claude Code CLI Reference](https://code.claude.com/docs/en/cli-reference)
- [Agent SDK Overview](https://platform.claude.com/docs/en/agent-sdk/overview)
- [Ralph Wiggum Plugin](https://github.com/anthropics/claude-code/blob/main/plugins/ralph-wiggum/README.md)
- [Continuous Claude](https://github.com/AnandChowdhary/continuous-claude)
- [Auto Mode Announcement](https://claude.com/blog/auto-mode)
- [Running Claude Code in a Loop (Anand Chowdhary)](https://anandchowdhary.com/blog/2025/running-claude-code-in-a-loop)
