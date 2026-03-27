# PoC 3: additionalContext Delivery Mechanism

## 결론

`additionalContext`는 Claude의 대화 컨텍스트에 문자열로 주입되는 필드다. "user message"나 "system message"가 아니라, Claude Code 내부에서 관리하는 별도의 컨텍스트 주입 경로를 통해 전달된다. 문서에서는 일관되게 **"String added to Claude's context"**라고만 표현하며, 구체적인 API-level 메시지 역할(user/system/assistant)은 명시하지 않는다.

핵심 요약:
- `additionalContext`는 **7개 이상의 hook event**에서 사용 가능하다
- 전달 방식은 이벤트마다 다르지만, 모두 "Claude's context에 추가"라는 동일한 결과를 낳는다
- **Stop hook은 `additionalContext` 필드가 없다** -- 대신 `decision: "block"` + `reason` 조합을 사용한다
- Plain text stdout과 JSON `additionalContext`는 전달 가시성이 다르다

## 전달 방식

### 두 가지 경로

1. **Plain text stdout** (exit code 0): stdout에 출력한 비-JSON 텍스트가 context로 추가된다. transcript에 "hook output"으로 표시된다.
2. **JSON `additionalContext` 필드** (exit code 0): hookSpecificOutput 내부에 포함. "더 조용하게(more discretely)" 추가된다고 문서에 명시.

> "Plain stdout is shown as hook output in the transcript. The `additionalContext` field is added more discretely."
> -- Hooks reference, UserPromptSubmit decision control

### 이벤트별 `additionalContext` 지원 현황

| Hook Event         | additionalContext 지원 | 위치                                  | 특이사항                                          |
|:-------------------|:----------------------|:--------------------------------------|:-------------------------------------------------|
| SessionStart       | O                     | `hookSpecificOutput.additionalContext` | 복수 hook의 값이 concatenate됨                     |
| UserPromptSubmit   | O                     | `hookSpecificOutput.additionalContext` | `decision: "block"`과 함께 사용 가능               |
| PreToolUse         | O                     | `hookSpecificOutput.additionalContext` | 도구 실행 전 Claude context에 추가                  |
| PostToolUse        | O                     | `hookSpecificOutput.additionalContext` | 도구 실행 후 Claude에게 피드백                      |
| PostToolUseFailure | O                     | `hookSpecificOutput.additionalContext` | 실패 정보와 함께 추가 컨텍스트                       |
| Notification       | O                     | `hookSpecificOutput.additionalContext` | block/modify 불가, context 추가만 가능              |
| SubagentStart      | O                     | `hookSpecificOutput.additionalContext` | **subagent의 context에** 추가 (메인 세션 아님)      |
| **Stop**           | **X**                 | N/A                                   | `decision: "block"` + `reason` 사용                |
| **SubagentStop**   | **X**                 | N/A                                   | Stop과 동일한 decision control 형식 사용             |

### "Context에 추가"의 실제 의미

문서는 API-level의 메시지 역할(user/system/assistant)을 명시하지 않는다. 다만 다음 단서들이 있다:

1. **`systemMessage`**: JSON output의 universal field로, "Warning message shown to the user"라고 설명. 이것은 사용자에게 표시되지만 Claude에게는 보이지 않는다.
2. **`additionalContext`**: "String added to Claude's context"라고 설명. Claude가 보고 행동할 수 있다.
3. **`reason` (Stop hook)**: "Tells Claude why it should continue"라고 설명. Claude에게 직접 전달된다.

따라서 `additionalContext`는 Claude가 실제로 "보는" 컨텍스트에 주입되며, 단순 UI 메시지가 아니다. 실질적으로는 **tool result나 user message에 해당하는 위치**에 주입될 가능성이 높다 (Claude API는 system/user/assistant/tool_result만 지원하므로).

## 크기 제한

**문서에 명시된 크기 제한은 없다.**

- `additionalContext`의 최대 길이에 대한 언급 없음
- stdout 출력량에 대한 제한 언급 없음
- hook 전체의 timeout은 기본 10분 (configurable via `timeout` field in seconds)

실질적 제한 요인:
1. Claude의 context window 크기 (모델에 따라 다름)
2. stdout 버퍼 크기 (OS 수준)
3. JSON 파싱 시 메모리 제한 (실질적으로는 매우 큼)

## Stop hook에서의 동작

### Stop hook은 `additionalContext`를 지원하지 않는다

Stop hook의 decision control 필드:

| Field      | Description                                                                |
|:-----------|:---------------------------------------------------------------------------|
| `decision` | `"block"` prevents Claude from stopping. Omit to allow Claude to stop      |
| `reason`   | Required when `decision` is `"block"`. Tells Claude why it should continue |

**`additionalContext` 필드가 Stop hook의 event-specific 필드에 없다.**

### `decision: "block"` + `reason`의 동작 원리

Stop hook에서 `decision: "block"`을 반환하면:

1. Claude가 **멈추지 않고 계속 작업한다**
2. `reason` 문자열이 **Claude에게 "왜 계속해야 하는지"를 알려주는 지시사항**으로 전달된다
3. Claude는 이 `reason`을 받아서 추가 작업을 수행한다

```json
{
  "decision": "block",
  "reason": "Tests are failing. Run npm test and fix the errors before stopping."
}
```

이 경우 Claude는 멈추지 않고, "Tests are failing. Run npm test and fix the errors before stopping."을 다음 지시로 받아들여 계속 작업한다.

### 무한 루프 방지

Stop hook input에는 `stop_hook_active` 필드가 포함된다:
- `false`: 일반 Stop (사용자 요청으로 Claude가 응답을 마침)
- `true`: Stop hook에 의해 continuation이 발생한 후의 Stop

```bash
#!/bin/bash
INPUT=$(cat)
if [ "$(echo "$INPUT" | jq -r '.stop_hook_active')" = "true" ]; then
  exit 0  # Allow Claude to stop (무한 루프 방지)
fi
# ... rest of your hook logic
```

### Agent/Prompt 기반 Stop hook

Agent hook (`type: "agent"`)이나 Prompt hook (`type: "prompt"`)을 Stop event에 사용할 경우:
- `{ "ok": false, "reason": "what remains to be done" }` 반환 시 Claude가 계속 작업
- `{ "ok": true }` 반환 시 Claude가 정상 종료
- `reason`이 실질적으로 `additionalContext`와 동일한 역할을 한다

```json
{
  "type": "prompt",
  "prompt": "Check if all tasks are complete. If not, respond with {\"ok\": false, \"reason\": \"what remains to be done\"}."
}
```

## 구조화된 지시사항 전달 가능 여부

**가능하다.** `additionalContext`는 string 타입이므로, 구조화된 텍스트를 포함할 수 있다:

```json
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "additionalContext": "REVIEW CHECKLIST:\n1. Check for SQL injection\n2. Verify input validation\n3. Ensure error handling is present\n\nCurrent environment: production. Proceed with caution."
  }
}
```

Stop hook의 `reason` 필드에도 동일하게 적용:

```json
{
  "decision": "block",
  "reason": "The following items are not yet complete:\n1. Unit tests are failing (3 failures)\n2. README not updated\n3. Lint errors in src/auth.ts\n\nPlease fix all issues before stopping."
}
```

## Async hook에서의 동작

비동기 hook(`async: true`)에서도 `additionalContext`가 동작한다:

> "After the background process exits, if the hook produced a JSON response with a `systemMessage` or `additionalContext` field, that content is delivered to Claude as context on the next conversation turn."

즉, 비동기 hook이 완료된 후 **다음 conversation turn**에서 context가 전달된다.

## 근거

### 공식 문서 인용

**1. additionalContext의 정의 (SessionStart)**
> "additionalContext: String added to Claude's context. Multiple hooks' values are concatenated"
> -- Hooks reference, SessionStart decision control

**2. additionalContext vs plain stdout (UserPromptSubmit)**
> "There are two ways to add context to the conversation on exit code 0:
> - Plain text stdout: any non-JSON text written to stdout is added as context
> - JSON with additionalContext: use the JSON format below for more control. The additionalContext field is added as context
>
> Plain stdout is shown as hook output in the transcript. The additionalContext field is added more discretely."
> -- Hooks reference, UserPromptSubmit decision control

**3. PreToolUse additionalContext**
> "additionalContext: String added to Claude's context before the tool executes"
> -- Hooks reference, PreToolUse decision control

**4. Stop hook의 reason 필드**
> "reason: Required when decision is 'block'. Tells Claude why it should continue"
> -- Hooks reference, Stop decision control

**5. Stop hook 무한 루프 방지**
> "The stop_hook_active field is true when Claude Code is already continuing as a result of a stop hook. Check this value or process the transcript to prevent Claude Code from running indefinitely."
> -- Hooks reference, Stop input

**6. Exit code 0의 동작 (SessionStart/UserPromptSubmit 특수)**
> "Exit 0 means success. [...] For most events, stdout is only shown in verbose mode (Ctrl+O). The exceptions are UserPromptSubmit and SessionStart, where stdout is added as context that Claude can see and act on."
> -- Hooks reference, Exit code output

**7. Async hook에서의 전달**
> "After the background process exits, if the hook produced a JSON response with a systemMessage or additionalContext field, that content is delivered to Claude as context on the next conversation turn."
> -- Hooks reference, How async hooks execute

**8. Prompt/Agent hook의 ok/reason 패턴**
> "If 'ok' is false, Claude continues working with the provided reason as its next instruction."
> -- Hooks reference, Example: Multi-criteria Stop hook

## 자율 에이전트 설계에의 시사점

### Stop hook 기반 자율 루프 설계

Agent hook을 Stop event에 연결하면:
1. 메인 Claude가 작업 완료를 시도할 때마다 agent hook이 검증
2. 미완료 항목이 있으면 `ok: false, reason: "remaining tasks..."` 반환
3. 메인 Claude가 reason을 지시로 받아 계속 작업
4. `stop_hook_active` 체크로 무한 루프 방지 (최대 1회 continuation만 허용하거나, 조건부 통과)

### additionalContext 활용 전략

| 전략                  | 적합한 Hook Event    | 용도                                    |
|:---------------------|:--------------------|:----------------------------------------|
| 세션 초기 지시 주입     | SessionStart        | 프로젝트 규칙, 현재 스프린트 정보         |
| 사용자 입력 보강       | UserPromptSubmit    | 프롬프트에 자동으로 컨텍스트 추가          |
| 도구 실행 전 경고      | PreToolUse          | 환경 정보, 주의사항 주입                   |
| 도구 실행 후 피드백    | PostToolUse         | 코드 품질 검사 결과 전달                   |
| 완료 전 검증          | Stop (reason 사용)  | 체크리스트 기반 검증 결과 전달              |
| 서브에이전트 지시      | SubagentStart       | 서브에이전트에 보안 가이드라인 주입          |
