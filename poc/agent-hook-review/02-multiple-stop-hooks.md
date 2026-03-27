# PoC 2: Multiple Stop Hooks Interaction

## 결론

Stop 이벤트에 `command`, `prompt`, `agent` 타입의 훅을 동시에 등록할 수 있다. 모든 매칭 훅은 **병렬(parallel)** 로 실행되며, 하나라도 "block"을 반환하면 Claude는 멈추지 않고 계속 작업한다. `additionalContext`는 여러 훅의 값이 **연결(concatenate)** 된다. 단, 한 훅의 output이 다른 훅의 input에 영향을 줄 수는 없다 -- 동시에 실행되기 때문이다.

## Q1: command + agent 훅을 같은 Stop 이벤트에 등록 가능한가?

**가능하다.**

Stop 이벤트는 `command`, `http`, `prompt`, `agent` 네 가지 타입을 모두 지원한다. 레퍼런스 문서에서 Stop이 모든 타입을 지원하는 이벤트 목록에 명시적으로 포함되어 있다.

설정 예시:
```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": ".claude/hooks/stop-check.sh"
          },
          {
            "type": "agent",
            "prompt": "Verify all tests pass before stopping. $ARGUMENTS",
            "timeout": 120
          }
        ]
      }
    ]
  }
}
```

내부 `hooks` 배열에 여러 핸들러를 넣으면 같은 matcher group 안에서 모두 실행된다. 별도의 matcher group으로 나누어도 된다(Stop은 matcher를 지원하지 않으므로 항상 전부 실행).

## Q2: 실행 순서

**순서 없음 -- 병렬(parallel) 실행.**

> "When an event fires, all matching hooks run in parallel, and identical hook commands are automatically deduplicated."
> -- Hooks guide (24.md, line 338)

> "All matching hooks run in parallel, and identical handlers are deduplicated automatically. Command hooks are deduplicated by command string, and HTTP hooks are deduplicated by URL."
> -- Hooks reference (35.md, line 333)

핵심:
- 모든 매칭 훅이 동시에 시작된다
- 실행 순서(ordering)를 보장하는 메커니즘은 문서에 없다
- 동일한 command 문자열은 자동 중복 제거(dedup)
- 동일한 URL의 HTTP 훅도 자동 중복 제거

## Q3: block vs allow 결정 병합 -- 어느 것이 이기는가?

**block이 이긴다 (block wins).**

문서에서 직접적으로 "block wins" 라고 명시한 문장은 없지만, 다음 사실들로 이를 확정할 수 있다:

1. **병렬 실행**: 모든 훅이 동시에 실행되고, Claude Code가 결과를 수집한다
2. **Stop 훅의 결정 패턴**: `decision: "block"` 또는 exit code 2로 차단. 생략하면 허용
3. **PreToolUse에서의 deny 우선 원칙**: deny 규칙이 allow 훅 결과보다 항상 우선한다고 명시

> "Returning 'allow' skips the interactive prompt but does not override permission rules. If a deny rule matches the tool call, the call is blocked even when your hook returns 'allow'."
> -- Hooks guide (24.md, line 443)

이 패턴은 전체 훅 시스템에 일관되게 적용된다: 허용(allow/omit)은 기본값이고, 차단(block/deny)은 적극적 결정이다. 하나라도 차단하면 차단된다.

prompt/agent 훅의 경우:
- `"ok": false` --> block에 해당
- `"ok": true` --> allow에 해당
- 여러 prompt/agent 훅 중 하나라도 `"ok": false`를 반환하면 Claude는 계속 작업한다

## Q4: 첫 번째 훅의 output이 두 번째 훅의 input에 영향을 주는가?

**아니오. 영향을 주지 않는다.**

병렬 실행이므로 각 훅은 동일한 원본 이벤트 데이터를 input으로 받는다. 한 훅이 다른 훅의 입력을 변경할 수 없다.

각 훅이 받는 Stop 이벤트 input:
```json
{
  "session_id": "abc123",
  "transcript_path": "~/.claude/projects/.../transcript.jsonl",
  "cwd": "/Users/...",
  "permission_mode": "default",
  "hook_event_name": "Stop",
  "stop_hook_active": true,
  "last_assistant_message": "I've completed the refactoring..."
}
```

모든 훅이 이 동일한 데이터를 동시에 받는다. 체인(chain) 실행이 아닌 팬아웃(fan-out) 모델이다.

## Q5: additionalContext 병합 방식

**연결(concatenation) 방식이다. last-one-win이 아니다.**

> `additionalContext`: "String added to Claude's context. Multiple hooks' values are concatenated"
> -- Hooks reference (35.md, line 693, SessionStart 문서)

이 문장은 SessionStart 이벤트의 additionalContext 설명에서 명시적으로 확인된다. 같은 패턴이 다른 이벤트에도 적용된다.

단, Stop 이벤트의 decision control 문서에는 `additionalContext` 필드가 명시되어 있지 않다. Stop 훅에서 사용 가능한 필드는:
- `decision`: `"block"` 또는 생략
- `reason`: block 시 Claude에게 보여줄 이유

PostToolUse, PreToolUse, SessionStart 등에서는 `additionalContext`가 공식 지원되며, 여러 훅의 값은 concatenate된다.

## 근거

### 병렬 실행 근거
```
"When an event fires, all matching hooks run in parallel, and identical hook
commands are automatically deduplicated."
```
-- 24.md (Hooks guide), line 338

```
"All matching hooks run in parallel, and identical handlers are deduplicated
automatically. Command hooks are deduplicated by command string, and HTTP
hooks are deduplicated by URL."
```
-- 35.md (Hooks reference), line 333

### Stop 이벤트가 모든 타입 지원
```
Events that support all four hook types (command, http, prompt, and agent):
- PermissionRequest
- PostToolUse
- PostToolUseFailure
- PreToolUse
- Stop
- SubagentStop
- TaskCompleted
- UserPromptSubmit
```
-- 35.md, lines 1807-1816

### Stop 훅 결정 제어
```
| decision | "block" prevents Claude from stopping. Omit to allow Claude to stop |
| reason   | Required when decision is "block". Tells Claude why it should continue |
```
-- 35.md, lines 1298-1301

### prompt/agent 훅 결정 형식
```
| ok     | true allows the action, false prevents it              |
| reason | Required when ok is false. Explanation shown to Claude |
```
-- 35.md, lines 1884-1887

### additionalContext 연결 방식
```
| additionalContext | String added to Claude's context. Multiple hooks' values are concatenated |
```
-- 35.md, line 693

### Stop 이벤트에 matcher 없음
```
UserPromptSubmit, Stop, TeammateIdle, TaskCompleted, WorktreeCreate, and
WorktreeRemove don't support matchers and always fire on every occurrence.
```
-- 35.md, line 210

### stop_hook_active로 무한 루프 방지
```
The stop_hook_active field is true when Claude Code is already continuing
as a result of a stop hook. Check this value or process the transcript
to prevent Claude Code from running indefinitely.
```
-- 35.md, line 1280

## 설정 구조 정리

### 여러 핸들러를 하나의 matcher group에 배치
```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          { "type": "command", "command": "echo check1" },
          { "type": "prompt", "prompt": "Check tasks. $ARGUMENTS" },
          { "type": "agent", "prompt": "Run tests. $ARGUMENTS" }
        ]
      }
    ]
  }
}
```

### 여러 matcher group으로 분리 (Stop에서는 의미 없지만 구조적으로 가능)
```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          { "type": "command", "command": "echo check1" }
        ]
      },
      {
        "hooks": [
          { "type": "agent", "prompt": "Run tests. $ARGUMENTS" }
        ]
      }
    ]
  }
}
```

두 방식 모두 동일하게 작동한다. Stop은 matcher를 지원하지 않으므로 모든 matcher group이 항상 매칭된다.

## 주의사항

1. **무한 루프 방지**: `stop_hook_active` 필드를 반드시 확인해야 한다. 이미 Stop 훅 결과로 계속 실행 중일 때 `true`가 된다.
2. **비용 고려**: prompt/agent 훅은 매 Stop 이벤트마다 LLM API를 호출한다. agent 훅은 최대 50턴까지 실행될 수 있어 비용이 누적된다.
3. **타임아웃 차이**: command 기본 600초, prompt 기본 30초, agent 기본 60초.
4. **dedup 주의**: 동일한 command 문자열의 훅은 자동으로 하나만 실행된다. 의도적으로 같은 command를 여러 번 실행하려면 약간 다르게 작성해야 한다.
