# PoC 1: Agent Hook on Stop Event

## 결론
**VERIFIED** -- `type: "agent"` hook은 Stop 이벤트에서 공식적으로 지원된다.

## 근거

### 1. Stop 이벤트는 4가지 hook type을 모두 지원한다

Hooks reference 문서에 명시적으로 나열되어 있다:

> Events that support all four hook types (`command`, `http`, `prompt`, and `agent`):
> - `PermissionRequest`
> - `PostToolUse`
> - `PostToolUseFailure`
> - `PreToolUse`
> - **`Stop`**
> - `SubagentStop`
> - `TaskCompleted`
> - `UserPromptSubmit`

`command`만 지원하는 이벤트 목록(ConfigChange, SessionStart, SessionEnd 등)에 Stop은 포함되지 않는다.

### 2. 공식 문서에 Stop + agent hook 예시가 존재한다

Hooks reference의 "Agent-based hooks" 섹션에 다음과 같은 예시가 있다:

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "agent",
            "prompt": "Verify that all unit tests pass. Run the test suite and check the results. $ARGUMENTS",
            "timeout": 120
          }
        ]
      }
    ]
  }
}
```

Hooks guide에서도 동일한 예시를 반복한다.

### 3. Agent hook은 Stop 이벤트 context를 수신한다

Stop hook의 입력 스키마는 hook type과 무관하게 동일하다:

```json
{
  "session_id": "abc123",
  "transcript_path": "~/.claude/projects/.../00893aaf-...jsonl",
  "cwd": "/Users/...",
  "permission_mode": "default",
  "hook_event_name": "Stop",
  "stop_hook_active": true,
  "last_assistant_message": "I've completed the refactoring. Here's a summary..."
}
```

Agent hook은 `$ARGUMENTS` placeholder를 통해 이 JSON을 prompt에 주입받는다. 따라서 `session_id`, `stop_hook_active`, `last_assistant_message` 모두 접근 가능하다.

### 4. Agent hook은 block decision을 반환할 수 있다

Agent hook은 prompt hook과 동일한 response schema를 사용한다:

> The response schema is the same as prompt hooks: `{ "ok": true }` to allow or `{ "ok": false, "reason": "..." }` to block.

`ok: false`가 반환되면 Claude Code는 이를 `decision: "block"`으로 처리한다:

> If `"ok"` is `false`, Claude continues working with the provided reason as its next instruction.

즉 agent hook이 `{ "ok": false, "reason": "테스트가 실패했다. src/auth.ts 수정 필요" }`를 반환하면, Claude는 멈추지 않고 해당 reason을 다음 지시사항으로 받아 작업을 계속한다.

### 5. additionalContext의 동작 방식 -- 제한사항 있음

여기서 주의할 점이 있다.

**Stop 이벤트의 decision control 필드는 `decision`과 `reason`뿐이다:**

| Field      | Description                                                                |
| :--------- | :------------------------------------------------------------------------- |
| `decision` | `"block"` prevents Claude from stopping. Omit to allow Claude to stop      |
| `reason`   | Required when `decision` is `"block"`. Tells Claude why it should continue |

Stop 이벤트에는 `additionalContext` 필드가 **문서에 명시되어 있지 않다**. `additionalContext`가 명시적으로 지원되는 이벤트는:
- `SessionStart` (hookSpecificOutput.additionalContext)
- `UserPromptSubmit` (additionalContext)
- `PreToolUse` (additionalContext)
- `PostToolUse` (additionalContext)
- `PostToolUseFailure` (additionalContext)
- `Notification` (additionalContext)
- `SubagentStart` (additionalContext)

**그러나 agent/prompt hook에서는 `reason` 필드가 사실상 additionalContext 역할을 한다.** Agent hook이 `ok: false`를 반환하면, `reason` 문자열이 Claude의 다음 지시사항으로 주입된다. 이것이 Stop 이벤트에서 agent hook이 context를 주입하는 메커니즘이다.

### 6. Agent hook의 작동 메커니즘

Agent hook이 fire되면:

1. Claude Code가 subagent를 spawn한다 (Read, Grep, Glob 등의 도구 사용 가능)
2. Subagent가 prompt와 hook input JSON을 받아 최대 50 turn까지 실행한다
3. Subagent가 `{ "ok": true/false, "reason": "..." }` 구조화된 JSON을 반환한다
4. Claude Code가 해당 decision을 prompt hook과 동일하게 처리한다

기본 timeout은 60초이며, 설정으로 변경 가능하다.

### 7. 무한 루프 방지

Stop hook 사용 시 반드시 `stop_hook_active` 필드를 확인해야 한다:

> The `stop_hook_active` field is `true` when Claude Code is already continuing as a result of a stop hook. Check this value or process the transcript to prevent Claude Code from running indefinitely.

Agent hook의 prompt에 이 조건을 포함시켜야 한다:

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "agent",
            "prompt": "Context: $ARGUMENTS\n\nIMPORTANT: If stop_hook_active is true in the context above, respond with {\"ok\": true} immediately to prevent infinite loops.\n\nOtherwise, verify that all requested tasks are complete by inspecting the codebase...",
            "timeout": 120
          }
        ]
      }
    ]
  }
}
```

## 검증 요약

| 질문 | 결과 |
|------|------|
| Stop hook에 `type: "agent"` 사용 가능? | YES -- 공식 문서에 명시 + 예시 존재 |
| Agent hook이 Stop event context 수신? | YES -- `$ARGUMENTS`로 session_id, stop_hook_active, last_assistant_message 접근 |
| Agent hook이 `decision: "block"` 반환 가능? | YES -- `ok: false`로 반환하면 block으로 처리됨 |
| Agent hook의 context 주입 가능? | PARTIAL -- `additionalContext` 필드는 Stop에 미지원, 대신 `reason`이 다음 지시사항으로 주입됨 |

## 대안 (additionalContext가 필요한 경우)

Stop 이벤트에서 `additionalContext`를 명시적으로 사용해야 한다면:

1. **`type: "command"` hook과 조합**: command hook에서 `decision: "block"`과 `reason`을 반환하되, block 전에 별도 로직으로 상태를 파일에 기록하고 SessionStart hook의 `additionalContext`로 주입하는 우회 방식
2. **agent hook의 `reason` 필드 활용**: `ok: false` 반환 시 `reason`에 충분한 context를 포함시키면, Claude가 해당 내용을 다음 작업의 지시사항으로 받는다. 이것이 가장 직접적인 방법이다.
3. **async command hook 병용**: Stop에서 async command hook으로 `systemMessage`나 `additionalContext`를 다음 turn에 전달할 수 있으나, 이 경우 Stop 자체를 block할 수 없다 (async hook은 decision control 불가).
