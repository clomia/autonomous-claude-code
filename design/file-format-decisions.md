# 파일 형식 결정서

이 문서는 시스템 내 모든 파일에 대한 형식 선택 근거를 기록한다.

---

## 1. 결정 원칙

### 1.1 형식 선택 기준

각 파일의 최적 형식은 다음 기준으로 결정한다:

1. **주 작성자** — AI(Claude Code)인가, Python(Supervisor)인가, 사람(Owner)인가?
2. **접근 패턴** — 전체 재작성인가, 부분 편집인가, 추가 전용인가?
3. **구조적 쿼리 필요성** — Python이 필터/정렬/집계를 해야 하는가?
4. **자기개선 대상** — Claude Code가 S-4/S-5로 수정하는가?
5. **성장 패턴** — 파일이 무한 성장하는가?

### 1.2 사용 가능한 형식

| 형식 | Python 지원 | AI 편집성 | 구조적 쿼리 | 코멘트 | 부분 편집 |
|------|------------|----------|------------|--------|----------|
| JSON | stdlib (json) | Write로 전체 재작성 | 최고 | 불가 | 불가 |
| TOML | stdlib 읽기 (tomllib), 쓰기 불필요 | Edit로 자연스러운 부분 편집 | 양호 | 가능 | 가능 |
| Markdown | 커스텀 파서 | Edit로 섹션별 편집 (최고) | 불가 | 가능 | 최고 |
| JSONL | stdlib (json) | 추가 전용 | 순차 스캔 | 불가 | 추가만 |

---

## 2. 파일별 결정

### 2.1 상태 파일 (state/)

| 파일 | 형식 | 핵심 근거 |
|------|------|----------|
| `purpose.json` | **JSON** | 구조적 메타데이터(evolution_history) 필요. 드물게 쓰임. |
| `strategy.json` | **JSON** | 혼합 구조(서술 + 배열). Python이 skill 고유성 검증. |
| `missions.json` | **JSON** | 구조적 쿼리 필수(priority 정렬, status 필터, 의존성 체크). 무한 성장 대응. |
| `friction.json` | **JSON** | pattern_key 카운팅, 임계값 체크. 무한 성장 대응. |
| `requests.json` | **JSON** | 구조적 매칭(thread_ts), status 필터. 느리게 성장. |
| `sessions.json` | **JSON** | Python만 쓰기. 구조적 집계. 무한 성장 대응. |
| `config.toml` | **TOML** | 자기개선(S-5) 시 인라인 코멘트로 변경 근거 기록. 아래 상세 분석. |

### 2.2 런타임 파일 (run/)

| 파일 | 형식 | 근거 |
|------|------|------|
| `current_session.json` | **JSON** | 임시 데이터, Python만 읽기/쓰기, 작고 평탄 |
| `supervisor.heartbeat` | **JSON** | 임시 데이터, Python/Watchdog만 사용 |
| `supervisor.state` | **JSON** | 크래시 복구용, Python만 읽기/쓰기 |

### 2.3 지시 파일

| 파일 | 형식 | 근거 |
|------|------|------|
| `CLAUDE.md` | **Markdown** | AI 네이티브. Edit 도구로 섹션별 자기개선. Claude Code가 자동 로드. |
| `.claude/rules/*.md` | **Markdown** | 같은 이유. @import로 CLAUDE.md에 포함. |
| `.claude/settings.json` | **JSON** | Claude Code 프로토콜이 JSON 요구. 선택 불가. |

### 2.4 기타

| 파일 | 형식 | 근거 |
|------|------|------|
| `logs/*.log` | **Plain text** | Python RotatingFileHandler. 표준 로깅. |
| Hook I/O | **JSON** | Claude Code Hook 프로토콜. 선택 불가. |
| `.env` | **dotenv** | 표준 환경변수 형식 |
| `pyproject.toml` | **TOML** | PEP 621 필수 |
| `setup/*.plist` | **XML plist** | macOS launchd 필수 |

---

## 3. config.toml 형식 선택 근거

### 3.1 TOML 선택 이유

config에 TOML 형식을 사용하는 핵심 이유:

**1. 자기개선(S-5)에서 인라인 코멘트의 가치**

Claude Code가 임계값을 수정할 때 변경 근거를 값 옆에 기록할 수 있다:

```toml
# 반복 에러 패턴이 2회에서 이미 명확함 (2026-03-28, F-023 참조)
friction_threshold = 2

# 10미션이 적절한 검토 주기임을 확인 (2026-04-01)
proactive_improvement_interval = 10

# 3에서 5로 상향: 컨텍스트 리프레시가 너무 자주 발생 (2026-03-30, F-019 참조)
context_refresh_after_compactions = 5
```

TOML 코멘트는 설정 값 옆에서 즉각적 맥락을 제공한다. friction.json의 변경 기록과 상호 보완적이다 — Claude Code가 다음 자기개선 시 config를 읽을 때, 이전 변경의 근거를 즉시 파악할 수 있다.

**2. Claude Code Edit 도구와의 호환성**

TOML의 `key = value` 형식은 Edit 도구로 자연스럽게 부분 편집된다:

```
old_string: "friction_threshold = 3"
new_string: "# 패턴이 2회에서 명확 (2026-03-28)\nfriction_threshold = 2"
```

JSON에서 같은 작업은 주변 구조(중괄호, 쉼표)까지 정확히 매칭해야 해서 취약하다.

**3. stdlib 읽기 지원**

Python 3.14에서 `tomllib`이 표준 라이브러리에 포함되어 있다. 읽기에 추가 의존성이 없다.

쓰기는 Python이 아닌 Claude Code(Write/Edit 도구)가 수행하므로 `tomli-w` 의존성이 불필요하다.

**4. 구조적 적합성**

config는 평탄한 key-value 구조로, TOML의 가장 강한 영역이다. 중첩 객체나 배열이 없다.

---

## 4. 아카이브 로테이션 전략

### 4.1 문제

세 파일이 무한 성장한다:
- `missions.json`: 완료/실패 미션이 영구 보존. 미션당 ~500바이트. 1000미션 시 ~500KB.
- `friction.json`: 모든 마찰 기록 영구 보존. 레코드당 ~300바이트.
- `sessions.json`: 모든 세션 기록 영구 보존. 레코드당 ~500바이트.

파일이 커지면:
- 매번 전체 읽기/쓰기하는 원자적 쓰기 비용 증가
- Hook의 파싱 시간 증가 (Stop Hook은 빠른 응답 필요)
- Git 저장소 크기 증가

### 4.2 해결: 활성/아카이브 분리

```
state/
├── missions.json          # 활성 미션만 (pending, in_progress, blocked)
├── friction.json          # 미해소 + 최근 N일 해소 마찰만
├── sessions.json          # 현재 + 최근 N개 세션만
└── archive/               # Git 추적. 수정 안 함.
    ├── missions-2026-Q1.jsonl   # 분기별 완료/실패 미션
    ├── friction-2026-Q1.jsonl   # 분기별 해소된 마찰
    └── sessions-2026-03.jsonl   # 월별 완료된 세션
```

### 4.3 아카이브 형식: JSONL

아카이브 파일은 **JSONL (JSON Lines)** 형식을 사용한다:
- 한 줄에 하나의 JSON 객체
- 추가 전용 (append-only) — 한번 쓰면 수정하지 않음
- 한 줄만 손상되어도 나머지는 읽을 수 있음
- Git diff가 깔끔함 (추가된 줄만 표시)

```jsonl
{"id":"M-001","title":"프로젝트 구조 설정","status":"completed","completed_at":"2026-03-25T12:00:00Z",...}
{"id":"M-002","title":"API 연동","status":"completed","completed_at":"2026-03-25T15:00:00Z",...}
```

### 4.4 로테이션 규칙

| 파일 | 로테이션 조건 | 활성 파일 보존 | 아카이브 단위 |
|------|-------------|---------------|-------------|
| missions | 완료/실패 미션 50개 초과 | pending + in_progress + blocked + 최근 완료 10개 | 분기별 |
| friction | 해소된 마찰 100개 초과 | 미해소 전체 + 최근 해소 20개 | 분기별 |
| sessions | 세션 기록 100개 초과 | 최근 20개 세션 | 월별 |

### 4.5 StateManager 메서드

```python
def rotate_missions(self) -> None:
    """완료/실패 미션을 아카이브로 이동"""

def rotate_friction(self) -> None:
    """해소된 마찰을 아카이브로 이동"""

def rotate_sessions(self) -> None:
    """오래된 세션 기록을 아카이브로 이동"""

def load_archive(self, entity: str, period: str) -> list[dict]:
    """아카이브에서 특정 기간의 레코드 로드 (자기개선 분석 시 사용)"""
```

---

## 5. JSON 파일의 근거 상세

### 5.1 purpose.json — JSON

**JSON이 적합한 이유:**
- `evolution_history`는 날짜/이유/이전값/새값을 가진 구조적 배열. Markdown으로 표현하면 파싱이 취약해짐.
- Python이 `constructed_at` 불변성, `evolution_history` 일관성을 검증해야 함.
- 파일이 극히 드물게 수정되므로 Edit 도구 편의성의 가치가 낮음.
- SessionStart Hook이 JSON을 읽어 Markdown 컨텍스트로 변환하는 비용이 미미함.

### 5.2 strategy.json — JSON

**JSON이 적합한 이유:**
- `approach`는 서술적이지만, `skills`는 고유성 검증이 필요한 구조적 배열.
- 혼합 구조(서술 + 배열 + 카운터)는 JSON이 더 적합.
- 자기개선 시 전체 재작성(Write 도구)이므로 Markdown의 부분 편집 이점이 없음.

### 5.3 missions.json — JSON

**JSON이 적합한 이유:**
- 구조적 쿼리 필수 (priority 정렬, status 필터, 의존성 체크). `json.load()` → dict 접근이 직관적.
- 미션은 복잡한 중첩 객체의 배열로, flat key-value(TOML)나 섹션 기반(Markdown)에 부적합.
- Claude Code의 텍스트 도구(Read/Edit/Write)로 직접 조작 가능해야 함.

### 5.4 friction.json — JSON

**JSON이 적합한 이유:**
- 마찰 해소 시 기존 레코드 수정 필요 (resolution 업데이트) → 추가 전용(JSONL) 부적합.
- pattern_key 카운팅, 임계값 체크 등 구조적 쿼리 필수.
- 아카이브는 JSONL 사용 (해소된 마찰은 더 이상 수정되지 않으므로).

---

## 6. 최종 형식 매트릭스

| 파일 | 형식 | 이유 요약 |
|------|------|----------|
| state/purpose.json | JSON | 구조적 메타데이터, Python 검증 |
| state/strategy.json | JSON | 혼합 구조, 배열 검증 |
| state/missions.json | JSON | 구조적 쿼리 필수 |
| state/friction.json | JSON | 패턴 카운팅, 임계값 체크 |
| state/requests.json | JSON | thread_ts 매칭 |
| state/sessions.json | JSON | Python 전용 쓰기 |
| state/config.toml | TOML | S-5 코멘트, flat key-value, stdlib |
| state/archive/*.jsonl | JSONL | 추가 전용 아카이브 |
| run/*.json | JSON | 임시, Python 전용 |
| CLAUDE.md, rules/*.md | Markdown | AI 네이티브, 자기개선 |
| .claude/settings.json | JSON | 프로토콜 필수 |
| logs/*.log | Plain text | 표준 로깅 |
| Hook I/O | JSON | 프로토콜 필수 |
