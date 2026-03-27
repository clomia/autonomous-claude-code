# 인지 부하 트리거 컴포넌트 설계

> **목적**: 모델에게 최대한의 유의미한 인지적 부하를 주어 추론 품질을 극대화한다 (Q-3).

---

## 1. 개요

모델은 지시된 범위 내에서 최소한의 부하로 완료를 선언하는 경향이 있다. 인간 검수자는 작업을 대강 훑고 의심 패턴을 찾아 모델이 탐색하지 않은 확률 분포를 트리거한다. 이 컴포넌트는 그 기능을 시스템적으로 구현한다.

두 계층의 인지 부하를 제공한다:

| 계층 | 메커니즘 | 원칙 준수 | 시점 |
|------|---------|----------|------|
| **자기 주도** | 미션 프롬프트의 다단계 프로토콜 | Q-4a, Q-4b, Q-4c, Q-4d | 세션 내 연속 |
| **외부 주입** | Stop hook agent가 패턴 분석 후 방향 제시 | Q-4a~Q-4e 전체 | 세션 종료 시도 시 |

자기 주도 계층은 모든 세션에서 기본 수준의 인지 부하를 보장한다. 외부 주입 계층은 수행자의 맹점을 외부 관점에서 보완한다.

---

## 2. 자기 주도 계층: 미션 프롬프트 프로토콜

### 2.1 구조

Supervisor가 미션 프롬프트를 생성할 때, 다단계 실행 프로토콜을 포함한다:

```
## 미션: {mission.id} {mission.title}

### 목표
{mission.description}

### 성공 기준
{mission.success_criteria — 항목별}

### 실행 프로토콜

이 미션을 다음 단계로 실행하라. 각 단계를 건너뛰지 마라.

**1단계 — 실행**: 성공 기준을 달성하라.

**2단계 — 검증**: 성공 기준 각 항목을 개별적으로 대조 확인하라.
  달성 여부가 불확실한 항목이 있으면 추가 작업하라.
  {인지 부하 모듈이 생성한 미션 특화 검증 지시}

**3단계 — 미탐색 영역**: 이 접근법의 약점 3가지를 식별하고 대응하라.
  {인지 부하 모듈이 생성한 미션 특화 확장 지시}

**4단계 — 요약**: state/session-summary.md에 다음을 기록하라:
  - 가장 불확실했던 결정 3가지와 근거
  - 검토했지만 채택하지 않은 대안
  - 타협한 부분과 이유
```

### 2.2 미션 특화 지시 생성

인지 부하 모듈(`system/cognitive_load.py`)이 2~3단계의 미션 특화 내용을 생성한다. Python 기반 결정론적 생성이다:

```python
class CognitiveLoadTrigger:
    """미션 프롬프트의 인지 부하 내용을 생성하고, 트리거 효과를 추적한다."""

    def generate_mission_protocol(
        self,
        mission: dict,
        health_metrics: dict,
        friction_history: list[dict],
    ) -> dict[str, list[str]]:
        """미션 특화 검증/확장 지시를 생성한다."""
        phase2 = []
        phase3 = []

        # 유사 미션의 friction 이력 기반
        related = [f for f in friction_history
                   if self._is_related(f, mission)]
        if related:
            types = set(f["type"] for f in related)
            phase2.append(
                f"유사 미션에서 {', '.join(types)} friction이 발생한 이력이 있다. "
                f"이 영역을 특히 주의하여 검증하라."
            )

        # 건강 메트릭 기반
        if health_metrics.get("friction_trend") == "increasing":
            phase3.append(
                "시스템 friction이 증가 추세이다. "
                "이 미션의 결과가 friction을 줄이는 방향인지 확인하라."
            )

        stalled = health_metrics.get("stalled_mission_id")
        if stalled == mission.get("id"):
            phase2.append(
                "이 미션이 이전 세션에서 정체되었다. "
                "이전과 다른 접근법을 시도하라."
            )

        return {"phase2": phase2, "phase3": phase3}
```

### 2.3 원칙 준수

자기 주도 계층은 Q-4e(컨텍스트 분리)를 충족하지 않는다 — 수행자 자신이 지시를 수행하므로 같은 컨텍스트이다. Q-4a(작업 기반 — friction 이력), Q-4b(미탐색 — 약점 식별 지시), Q-4c(비처방 — 방향 제시), Q-4d(컨텍스트 내)는 충족한다. Q-4e는 외부 주입 계층이 담당한다.

---

## 3. 외부 주입 계층: Stop Hook Agent

### 3.1 작동 원리

수행자가 세션 종료를 시도하면 Stop hook이 발동한다. `type: "agent"` 훅이 **별도 컨텍스트**에서 실행되어 수행자의 작업 패턴을 분석하고 방향을 제시한다.

```
수행자: 작업 완료, 종료 시도
    │
    ▼
Stop hook agent (별도 컨텍스트, opus):
    │ 읽는 것:
    │   1. run/session-analysis.json ← 작업 패턴 (객관적 사실)
    │   2. 변경된 파일들 ← 실제 결과물
    │   3. 미션 success_criteria
    │   4. state/trigger-effectiveness.jsonl ← 이전 트리거의 효과 기록
    │
    │ 읽지 않는 것:
    │   - 수행자의 추론, 해석, 계획, 설명
    │
    │ 분석 → 방향 생성
    │
    ▼
ok: false, reason: "탐색하라: ..."
    │
    ▼
수행자: 같은 컨텍스트에서 방향을 받고 추가 작업
    │ (1~4단계의 전체 작업 기억 보존)
    │
    ▼
수행자: 추가 완료 → stop_hook_active: true → 세션 종료
```

### 3.2 session-analysis.json (작업 패턴 기록)

Supervisor의 StreamAnalyzer가 stream-json에서 실시간 추출하는 객관적 기록이다.

```python
class StreamAnalyzer:
    """stream-json 이벤트를 실시간 분석하여 작업 패턴을 기록한다."""

    def __init__(self):
        self.tool_calls: list[dict] = []
        self.errors: list[dict] = []
        self.files_read: set[str] = set()
        self.files_written: set[str] = set()
        self.bash_commands: list[dict] = []
        self.start_time: float = time.time()

    def process_event(self, event: dict) -> None:
        """각 stream-json 이벤트를 처리한다."""
        if event.get("type") == "tool_use":
            tool = event.get("tool", "")
            target = event.get("target", "")
            self.tool_calls.append({
                "tool": tool,
                "target": target,
                "timestamp": time.time() - self.start_time,
            })
            if tool == "Read":
                self.files_read.add(target)
            elif tool in ("Edit", "Write"):
                self.files_written.add(target)
            elif tool == "Bash":
                self.bash_commands.append({
                    "command": event.get("command", "")
                })

        elif event.get("type") == "tool_result" and event.get("is_error"):
            self.errors.append({
                "tool": self.tool_calls[-1]["tool"] if self.tool_calls else "unknown",
                "error": str(event.get("text", ""))[:300],
                "timestamp": time.time() - self.start_time,
            })

    def write_analysis(self, path: Path) -> None:
        """작업 패턴을 파일에 기록한다."""
        analysis = {
            "tool_call_count": len(self.tool_calls),
            "tool_distribution": dict(Counter(
                tc["tool"] for tc in self.tool_calls
            )),
            "files_read": sorted(self.files_read),
            "files_written": sorted(self.files_written),
            "files_read_not_written": sorted(
                self.files_read - self.files_written
            ),
            "errors": self.errors,
            "error_count": len(self.errors),
            "tests_executed": any(
                "pytest" in cmd.get("command", "")
                or "test" in cmd.get("command", "")
                for cmd in self.bash_commands
            ),
            "bash_commands": [
                cmd["command"][:100] for cmd in self.bash_commands
            ],
            "duration_minutes": round(
                (time.time() - self.start_time) / 60, 1
            ),
            "topic_areas": self._extract_topic_areas(),
        }
        atomic_write(path, json.dumps(
            analysis, ensure_ascii=False, indent=2
        ))

    def _extract_topic_areas(self) -> dict[str, int]:
        """파일 경로에서 작업 영역을 추출한다."""
        areas: dict[str, int] = {}
        for f in self.files_read | self.files_written:
            parts = Path(f).parts
            if len(parts) >= 2:
                area = str(Path(*parts[:2]))
                areas[area] = areas.get(area, 0) + 1
        return areas
```

**포함**: tool call 목록, 파일 접근 기록, 에러, bash 명령, 시간 분포, 작업 영역.
**제외**: assistant 텍스트, 도구 결과 내용, 수행자의 추론/해석.

### 3.3 Stop Hook Agent Prompt

`.claude/settings.json`에 등록한다. prompt는 5개 독립 설계에서 도출된 분석 기법을 통합한다:

```json
{
  "Stop": [
    {
      "hooks": [
        {
          "type": "agent",
          "model": "opus",
          "timeout": 120,
          "tools": ["Read", "Glob", "Grep", "Bash"],
          "prompt": "당신은 인지 부하 트리거 생성자이다.\n\n메인 에이전트가 미션을 수행하고 완료를 선언했다. 당신의 역할은 메인 에이전트가 탐색하지 않았을 가능성이 높은 방향을 식별하는 것이다.\n\n## 입력\n\n다음 파일들을 읽어라:\n1. run/session-analysis.json — 수행된 작업의 객관적 패턴 기록\n2. state/missions.json에서 status가 in_progress인 미션의 success_criteria\n3. session-analysis.json의 files_written에 나열된 파일들\n4. state/trigger-effectiveness.jsonl (존재하면)\n\n## 분석 방법\n\n다음 렌즈로 작업 패턴을 분석하라:\n\n**부재 분석**: success_criteria에 있지만 작업 기록에 반영되지 않은 것은 무엇인가? 접근되지 않은 파일, 실행되지 않은 명령, 탐색되지 않은 영역은?\n\n**접근법 다양성**: 수행자가 하나의 도구 유형이나 하나의 작업 패턴만 사용했는가? 읽기만 하고 테스트하지 않았는가? 작성만 하고 검증하지 않았는가?\n\n**에러 무시 패턴**: 에러가 발생한 후 수행자가 조사하지 않고 넘어간 것이 있는가?\n\n**깊이 비대칭**: 어떤 영역은 깊이 탐색하고 다른 영역은 피상적으로만 다룬 것이 있는가?\n\n**수렴 패턴**: 작업이 점점 좁은 범위로 수렴했는가? 초기 탐색에서 발견했지만 후속 작업에 반영하지 않은 것은?\n\n## 이전 트리거 효과\n\ntrigger-effectiveness.jsonl이 존재하면 읽어라. 효과가 높았던 분석 유형을 우선시하라. 효과가 낮았던 유형은 다른 접근을 시도하라.\n\n## 출력 규칙\n\n- 결론을 내리거나 해법을 처방하지 마라. 탐색 방향만 제시하라.\n- 각 방향은 session-analysis.json의 구체적 패턴을 근거로 들어라.\n- 수행자가 이미 충분히 다룬 영역은 반복하지 마라.\n- 최대 3개 방향을 제시하라.\n- $ARGUMENTS의 last_assistant_message는 수행자의 해석이다. 무시하고 객관적 패턴만 참조하라."
        }
      ]
    }
  ]
}
```

### 3.4 원칙 준수

| 원칙 | 충족 방식 |
|------|----------|
| Q-4a 작업 기반 | session-analysis.json(작업 패턴)과 실제 파일을 읽음 |
| Q-4b 미탐색 지향 | 분석 방법이 "부재", "미탐색", "미접근"을 명시적으로 탐색. effectiveness 이력으로 반복 방지 |
| Q-4c 비처방 | "결론을 내리지 마라. 방향만 제시하라" 명시 |
| Q-4d 컨텍스트 내 전달 | Stop hook reason이 메인 세션에 주입. 수행자의 전체 작업 기억 보존 |
| Q-4e 컨텍스트 분리 | 별도 컨텍스트에서 실행. session-analysis.json은 사실만 포함. assistant 텍스트 제외 |

---

## 4. 효과 추적 (자기개선의 씨앗)

### 4.1 목적

트리거가 수행자의 행동을 실제로 변화시켰는지 측정한다. 이 데이터가 없으면 트리거 시스템은 영원히 정적이다. 이 데이터가 있으면 자기개선이 "어떤 유형의 트리거가 효과적인가?"를 판단할 수 있다.

### 4.2 측정

```
트리거 전: session-analysis.json 스냅샷 저장 (pre_snapshot)
트리거 주입: Stop hook reason 전달
수행자: 추가 작업
세션 종료: session-analysis.json 최종 상태 (post_snapshot)

효과 = post_snapshot - pre_snapshot
```

```python
def compute_effectiveness(pre: dict, post: dict) -> str:
    new_files = len(
        set(post["files_read"] + post["files_written"])
        - set(pre["files_read"] + pre["files_written"])
    )
    new_areas = len(
        set(post.get("topic_areas", {}))
        - set(pre.get("topic_areas", {}))
    )
    calls_after = post["tool_call_count"] - pre["tool_call_count"]

    if new_files >= 3 or new_areas >= 1:
        return "high"
    elif new_files >= 1 or calls_after >= 5:
        return "medium"
    else:
        return "low"
```

### 4.3 기록

`state/trigger-effectiveness.jsonl` (append-only, Git 추적):

```jsonl
{"mission_id":"M-042","trigger_directions":["에러 처리 경로 탐색","테스트 커버리지 확인"],"effectiveness":"high","new_files":5,"new_areas":2,"timestamp":"2026-03-27T10:00:00Z"}
```

Stop hook agent의 prompt가 이 파일을 읽도록 지시되어 있다. 미션이 축적될수록 agent hook이 효과적인 분석 유형을 우선시하게 된다.

---

## 5. 컨텍스트 흐름

```
세션 시작 (미션 프롬프트 + 4단계 프로토콜)
    │
    ├── 1단계: 실행 (turns 1~N)
    │     Supervisor: stream-json → session-analysis.json 실시간 갱신
    │
    ├── 2단계: 검증 (1단계 컨텍스트 위에서)
    │
    ├── 3단계: 미탐색 영역 (1+2단계 컨텍스트 위에서)
    │
    ├── 4단계: 요약 (1+2+3단계 컨텍스트 위에서)
    │
    ▼
수행자 "완료" → Stop hook 발동
    │ Supervisor: pre_trigger 스냅샷 저장
    │
    ▼
Stop hook agent (opus, 120s, 별도 컨텍스트)
    │ 분석 → 방향 생성
    ▼
ok: false, reason: [방향들]
    │
    ▼
수행자: 추가 작업 (전체 컨텍스트 보존)
    │
    ▼
stop_hook_active: true → 세션 종료
    │ Supervisor: post_trigger 스냅샷
    │ 효과 측정 → trigger-effectiveness.jsonl에 기록
    ▼
Supervisor: 다음 세션
```

---

## 6. 구성 요소

| 구성 요소 | 위치 | 역할 |
|----------|------|------|
| CognitiveLoadTrigger | `system/cognitive_load.py` | 미션 특화 지시 생성 + 효과 추적 |
| StreamAnalyzer | `system/cognitive_load.py` | stream-json → session-analysis.json |
| Stop hook agent | `.claude/settings.json` | 외부 컨텍스트에서 패턴 분석 → 방향 생성 |
| session-analysis.json | `run/` | 작업 패턴의 객관적 기록 |
| trigger-effectiveness.jsonl | `state/` | 트리거 효과 이력 |

---

## 7. 자기개선 경로

이 컴포넌트의 모든 부분은 자기개선 대상이다 (S-4):

| 대상 | 방법 |
|------|------|
| Stop hook agent prompt | `.claude/settings.json` 수정. 분석 방법 추가/변경 |
| 미션 프로토콜 | `system/cognitive_load.py`의 generate_mission_protocol() 수정 |
| StreamAnalyzer | 새 패턴 추출 로직 추가 |
| 효과 측정 로직 | 새 효과 지표 추가 |
| 4단계 프로토콜 구조 | 단계 추가/변경 |

trigger-effectiveness.jsonl이 자기개선의 데이터 기반이다. AI가 이 데이터를 분석하여 "absence 분석의 효과가 높고 convergence 분석은 낮다"를 발견하면, Stop hook prompt를 수정하여 absence 분석을 강화한다.

---

## 8. Q-3/Q-4 준수 검증

| 요구사항 | 충족 |
|----------|------|
| Q-3 최대 인지 부하 | 자기 주도(4단계) + 외부 주입(Stop hook agent) = 5회 이상 인지 전환, 같은 컨텍스트에서 축적 |
| Q-4a 작업 기반 | session-analysis.json + 실제 파일 읽기 |
| Q-4b 미탐색 지향 | 부재/다양성/에러무시/비대칭/수렴 분석. effectiveness 이력으로 반복 방지 |
| Q-4c 비처방 | prompt에 "방향만 제시" 명시 |
| Q-4d 컨텍스트 내 전달 | Stop hook reason이 동일 세션에 주입 |
| Q-4e 컨텍스트 분리 | 별도 컨텍스트. 사실만 입력. 해석 제외 |
