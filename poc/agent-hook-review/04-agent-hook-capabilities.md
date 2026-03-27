# PoC 4: Agent Hook Capabilities and Limitations

## Timeout

- **기본값**: 60초 (agent hook), 30초 (prompt hook), 600초/10분 (command hook)
- **설정 가능**: `timeout` 필드로 초 단위 커스텀 가능
- 60초는 기본값이지 상한이 아님. 문서 예제에서 `"timeout": 120`을 사용하는 경우가 있음
- command hook의 10분 제한이 사실상 시스템 상한으로 보이나, agent hook에 대한 명시적 상한 언급은 없음

```json
{
  "type": "agent",
  "prompt": "Verify tests pass. $ARGUMENTS",
  "timeout": 120
}
```

## 도구 접근

문서에서 명시적으로 언급하는 도구:
- **Read** - 파일 읽기
- **Grep** - 코드 검색
- **Glob** - 파일 패턴 매칭

문서 표현: *"tools like Read, Grep, and Glob"* ("like"라는 표현은 이것이 전체 목록이 아님을 시사)

추가 도구 접근 가능성:
- 가이드 문서의 agent hook 예제에서 **"Run the test suite and check the results"**라는 프롬프트를 사용 -- 이는 **Bash** 도구 접근을 암시
- 가이드에서도 *"inspecting files or running commands"*라고 agent hook을 설명
- agent hook은 subagent를 spawn하므로, 해당 subagent가 사용할 수 있는 도구 범위는 일반 subagent와 유사할 가능성이 높음

**결론**: Read, Grep, Glob은 확실. Bash도 사용 가능할 가능성이 높음 (문서 예제가 테스트 실행을 전제). Write/Edit은 agent hook의 "검증" 목적상 사용 가능 여부 불명확.

## Turn 제한

- **최대 50턴** (tool-use turns)
- 문서 원문: *"After up to 50 turns, the subagent returns a structured `{ "ok": true/false }` decision"*
- 50턴이면 복잡한 검증 로직도 충분히 수행 가능 (파일 읽기 + 검색 + 테스트 실행 등)

## Prompt 동적 생성

### 정적 prompt (설정 파일에 하드코딩)
`prompt` 필드는 settings.json에 문자열로 정의해야 함. 파일에서 동적으로 읽어오는 메커니즘은 없음.

### $ARGUMENTS placeholder
- `$ARGUMENTS`를 prompt에 포함하면 hook의 JSON input 데이터로 치환됨
- `$ARGUMENTS`가 prompt에 없으면, input JSON이 prompt 끝에 자동 append됨
- 이것이 유일한 동적 요소

### 우회 방법: agent hook 자체가 파일을 읽을 수 있음
prompt 자체는 정적이지만, agent hook의 subagent가 Read 도구로 파일을 읽을 수 있으므로:

```json
{
  "type": "agent",
  "prompt": "Read the file review-instructions.md in the .claude directory, then evaluate the code changes based on those instructions. $ARGUMENTS"
}
```

이렇게 하면 agent가 실행 시점에 동적으로 생성된 파일의 내용을 읽어서 판단할 수 있음.
**prompt 자체는 정적이지만, agent의 행동은 동적일 수 있다.**

### command hook과의 조합
command hook에서는 `command` 필드에서 셸 명령어를 실행하므로 완전한 동적 생성이 가능:
```json
{
  "type": "command",
  "command": "cat .claude/review-instructions.md | my-evaluation-script.sh"
}
```

## 모델

- **기본값**: "a fast model" (Haiku로 추정 -- 가이드 문서에서 *"Haiku by default"*라고 명시)
- **설정 가능**: `model` 필드로 변경 가능
- 문서 원문: *"Defaults to a fast model"*, *"You can specify a different model with the `model` field if you need more capability"*

```json
{
  "type": "agent",
  "prompt": "...",
  "model": "claude-sonnet-4-20250514"
}
```

주의: 기본값이 Haiku이므로, 복잡한 판단이 필요한 경우 반드시 model을 명시적으로 지정해야 함.

## 제한사항

### 지원 이벤트 제한
agent/prompt hook을 사용할 수 있는 이벤트는 8개만:
- `PermissionRequest`
- `PostToolUse`
- `PostToolUseFailure`
- `PreToolUse`
- `Stop`
- `SubagentStop`
- `TaskCompleted`
- `UserPromptSubmit`

다음 이벤트에서는 **command hook만** 사용 가능:
- `ConfigChange`, `Elicitation`, `ElicitationResult`, `InstructionsLoaded`
- `Notification`, `PostCompact`, `PreCompact`, `SessionEnd`, `SessionStart`
- `StopFailure`, `SubagentStart`, `TeammateIdle`, `WorktreeCreate`, `WorktreeRemove`

### 비동기 실행 불가
- `async: true`는 command hook에서만 지원
- agent/prompt hook은 항상 동기(blocking) 실행

### 응답 형식 고정
- `{ "ok": true }` 또는 `{ "ok": false, "reason": "..." }` 형식만 반환 가능
- command hook의 `hookSpecificOutput` 같은 세밀한 제어 불가

### Subagent 중첩 불명확
- agent hook이 다른 subagent를 spawn할 수 있는지 문서에 명시되어 있지 않음
- agent hook 자체가 subagent를 spawn하는 것이므로, 중첩 spawn은 제한될 가능성이 높음

### 타임아웃 발생 시
- 문서에 agent hook 타임아웃 시의 정확한 동작이 명시되어 있지 않음
- command hook의 경우, 타임아웃은 비차단 에러로 처리되어 실행이 계속됨
- agent hook도 유사하게 동작할 것으로 추정 (타임아웃 시 action이 proceed)

## prompt vs agent 훅 비교

| 항목 | `type: "prompt"` | `type: "agent"` |
|------|-------------------|-------------------|
| **실행 방식** | 단일 LLM 호출 (single-turn) | 다중 턴 subagent (multi-turn) |
| **도구 접근** | 없음 (입력 데이터만 평가) | Read, Grep, Glob 등 도구 사용 가능 |
| **기본 timeout** | 30초 | 60초 |
| **최대 턴 수** | 1 (단일 호출) | 최대 50턴 |
| **기본 모델** | Haiku (fast model) | Haiku (fast model) |
| **model 설정** | 가능 | 가능 |
| **응답 형식** | `{ "ok": true/false, "reason": "..." }` | 동일 |
| **지원 이벤트** | 동일 8개 | 동일 8개 |
| **async 지원** | 불가 | 불가 |
| **비용** | 낮음 (단일 API 호출) | 높음 (다중 API 호출 + 도구 사용) |
| **사용 시기** | 입력 데이터만으로 판단 가능할 때 | 파일 확인, 코드 검색, 테스트 실행 등 코드베이스 검증이 필요할 때 |

### 선택 기준
- **prompt hook**: hook input JSON만으로 판단 가능 (예: 커밋 메시지 검증, 명령어 패턴 매칭)
- **agent hook**: 실제 코드베이스 상태 확인 필요 (예: 테스트 통과 여부, 파일 존재 확인, 코드 리뷰)

## 근거

### Hooks Reference (35.md)

timeout 기본값:
> `timeout` - Seconds before canceling. Defaults: 600 for command, 30 for prompt, 60 for agent

agent hook 정의:
> Agent hooks (`type: "agent"`): spawn a subagent that can use tools like Read, Grep, and Glob to verify conditions before returning a decision.

agent hook 작동 방식:
> 1. Claude Code spawns a subagent with your prompt and the hook's JSON input
> 2. The subagent can use tools like Read, Grep, and Glob to investigate
> 3. After up to 50 turns, the subagent returns a structured `{ "ok": true/false }` decision
> 4. Claude Code processes the decision the same way as a prompt hook

모델 설정:
> `model` - Model to use. Defaults to a fast model

$ARGUMENTS:
> `prompt` - Prompt describing what to verify. Use `$ARGUMENTS` as a placeholder for the hook input JSON

### Hooks Guide (24.md)

agent hook 개요:
> When verification requires inspecting files or running commands, use `type: "agent"` hooks. Unlike prompt hooks which make a single LLM call, agent hooks spawn a subagent that can read files, search code, and use other tools to verify conditions before returning a decision.

턴 제한:
> Agent hooks use the same `"ok"` / `"reason"` response format as prompt hooks, but with a longer default timeout of 60 seconds and up to 50 tool-use turns.

prompt hook 모델:
> Claude Code sends your prompt and the hook's input data to a Claude model (Haiku by default) to make the decision. You can specify a different model with the `model` field if you need more capability.

prompt hook vs agent hook 사용 기준:
> Use prompt hooks when the hook input data alone is enough to make a decision. Use agent hooks when you need to verify something against the actual state of the codebase.

## 핵심 인사이트

1. **Agent hook은 강력하지만 비용이 높다**: 매 Stop마다 최대 50턴의 subagent가 도는 것은 API 비용이 상당할 수 있음. timeout과 사용 빈도를 신중히 설계해야 함.

2. **Prompt은 정적이지만 행동은 동적**: prompt 문자열 자체는 settings.json에 고정되지만, agent가 Read로 파일을 읽을 수 있으므로 실질적으로 동적 지시가 가능.

3. **기본 모델이 Haiku**: 복잡한 코드 리뷰나 판단에는 부족할 수 있음. `model` 필드로 Sonnet 이상을 지정하는 것을 권장.

4. **Stop hook 무한 루프 위험**: Stop hook에서 `"ok": false`를 반환하면 Claude가 계속 작업함. `stop_hook_active` 필드를 체크하는 로직이 필요 (command hook의 경우). prompt/agent hook에서 이를 어떻게 처리하는지는 불명확.

5. **Command hook과 조합이 최적**: 동적 프롬프트가 필요하면 command hook에서 프롬프트 파일을 읽어 처리하고, 코드베이스 검증이 필요하면 agent hook을 사용하는 하이브리드 접근이 효과적.
