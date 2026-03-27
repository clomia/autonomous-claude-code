# Error Classifier 컴포넌트 설계서

> **파일**: `system/error_classifier.py`
> **책임**: 에러 분류, 복구 전략 선택, 연속 실패 추적, 백오프 계산
> **참조 요구사항**: E-2 (장애 불멸), E-3 (장애 분류)

---

## 1. 개요

ErrorClassifier는 Claude Code 세션의 종료 상태를 분석하여 에러 유형을 분류하고, 각 유형에 맞는 복구 전략을 반환한다. Supervisor의 세션 루프에서 세션이 비정상 종료될 때마다 호출되어, 시스템이 자율적으로 장애를 극복할 수 있게 한다.

### 설계 원칙

1. **결정론적 분류**: 같은 입력에 대해 항상 같은 에러 유형을 반환한다
2. **점진적 에스컬레이션**: 반복 실패 시 더 강력한 복구 전략으로 단계적 전환
3. **자기 치유 우선**: Owner 알림은 마지막 수단. 가능한 한 자동 복구를 시도한다
4. **관측 가능성**: 모든 분류 결과와 복구 시도를 기록하여 추후 자기개선에 활용한다

---

## 2. 에러 유형 (Error Types)

```python
from enum import Enum


class ErrorType(Enum):
    """
    Claude Code 세션 에러 유형.

    각 유형은 고유한 탐지 조건과 복구 전략을 가진다.
    분류 우선순위는 enum 정의 순서를 따른다 (위가 높음).
    """
    RATE_LIMITED = "rate_limited"            # API rate limit (429)
    AUTH_FAILURE = "auth_failure"            # 인증/권한 오류 (401/403)
    TRANSIENT_API = "transient_api"          # 일시적 API 오류 (500/502/503)
    NETWORK_ERROR = "network_error"          # 네트워크 연결 오류
    CONTEXT_CORRUPTION = "context_corruption"  # 컨텍스트/출력 손상
    STUCK = "stuck"                          # 세션 무응답 (타임아웃)
    PROCESS_CRASH = "process_crash"          # 프로세스 비정상 종료
    UNKNOWN = "unknown"                      # 분류 불가
```

### 에러 유형별 탐지 조건 및 복구 전략

| 유형 | 탐지 조건 | 복구 전략 | 에스컬레이션 |
|------|-----------|-----------|-------------|
| `RATE_LIMITED` | stream event에 `api_retry` 존재, stderr에 "429" | 이벤트의 `retry_after` 값만큼 대기 후 세션 재개 | 5회 초과 → `notify_owner` |
| `AUTH_FAILURE` | stderr에 "401", "403", "auth", "login", "unauthorized" | Owner에 알림, 수동 해결 대기 | 즉시 에스컬레이션 |
| `TRANSIENT_API` | exit code != 0 + stderr에 "500", "502", "503", "internal server error" | 지수 백오프 (1s→2s→4s→8s, 최대 60s), 최대 5회 재시도 | 5회 초과 → `notify_owner` |
| `NETWORK_ERROR` | stderr에 "connection", "timeout", "ECONNREFUSED", "ENOTFOUND", "network" | 지수 백오프, 최대 10회 재시도 (10분 이내) | 10회 초과 → `notify_owner` |
| `CONTEXT_CORRUPTION` | JSON 파싱 에러, 비정상 출력 (garbled), stream 이벤트 불완전 | 마지막 체크포인트에서 fresh session 시작 | 3회 초과 → `notify_owner` |
| `STUCK` | `session_timeout_minutes` 동안 이벤트 없음 | 프로세스 강제 종료 + fresh session + friction 기록 | 3회 초과 → `notify_owner` |
| `PROCESS_CRASH` | exit code != 0, 정상 종료 이벤트 없음, 위 조건 해당 없음 | friction 기록 + fresh session (상태 복구 포함) | 3회 초과 → `notify_owner` |
| `UNKNOWN` | 위 조건 모두 해당 없음 | 1회 재시도, 실패 시 Owner 알림 | 2회 초과 → `notify_owner` |

---

## 3. 복구 전략 (Recovery Strategy)

```python
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RecoveryStrategy:
    """
    에러 복구 전략.

    ErrorClassifier가 에러 유형에 따라 적절한 전략을 반환한다.
    Supervisor의 SessionManager가 이 전략에 따라 복구 동작을 수행한다.

    Attributes:
        action: 복구 동작 유형
            - "retry_resume": 기존 세션을 --resume으로 재개 시도
            - "retry_fresh": 새 세션을 시작 (상태 복구 후)
            - "wait_and_resume": 지정 시간 대기 후 세션 재개
            - "notify_owner": Owner에 Slack 알림 후 대기
            - "checkpoint_restore": 체크포인트 복원 후 새 세션
        delay_seconds: 재시도 전 대기 시간 (초)
        max_retries: 이 전략의 최대 재시도 횟수
        escalate_after: N회 실패 후 다음 전략으로 에스컬레이션
        next_strategy: 에스컬레이션 시 전환할 action. None이면 notify_owner가 최종.
    """
    action: str
    delay_seconds: float
    max_retries: int
    escalate_after: int
    next_strategy: str | None


# ── 에러 유형별 기본 복구 전략 ──

RECOVERY_STRATEGIES: dict[ErrorType, RecoveryStrategy] = {
    ErrorType.TRANSIENT_API: RecoveryStrategy(
        action="retry_resume",
        delay_seconds=1.0,       # 지수 백오프의 base. 실제 delay는 backoff 계산기가 결정.
        max_retries=5,
        escalate_after=5,
        next_strategy="notify_owner",
    ),
    ErrorType.RATE_LIMITED: RecoveryStrategy(
        action="wait_and_resume",
        delay_seconds=0.0,       # retry_after 값을 사용. 기본값은 placeholder.
        max_retries=10,
        escalate_after=5,
        next_strategy="notify_owner",
    ),
    ErrorType.AUTH_FAILURE: RecoveryStrategy(
        action="notify_owner",
        delay_seconds=0.0,
        max_retries=0,           # 재시도 없음. Owner가 해결해야 함.
        escalate_after=1,
        next_strategy=None,
    ),
    ErrorType.CONTEXT_CORRUPTION: RecoveryStrategy(
        action="checkpoint_restore",
        delay_seconds=2.0,
        max_retries=3,
        escalate_after=3,
        next_strategy="notify_owner",
    ),
    ErrorType.PROCESS_CRASH: RecoveryStrategy(
        action="retry_fresh",
        delay_seconds=5.0,
        max_retries=3,
        escalate_after=3,
        next_strategy="notify_owner",
    ),
    ErrorType.NETWORK_ERROR: RecoveryStrategy(
        action="retry_resume",
        delay_seconds=2.0,       # 지수 백오프의 base
        max_retries=10,
        escalate_after=10,
        next_strategy="notify_owner",
    ),
    ErrorType.STUCK: RecoveryStrategy(
        action="retry_fresh",
        delay_seconds=10.0,
        max_retries=3,
        escalate_after=3,
        next_strategy="notify_owner",
    ),
    ErrorType.UNKNOWN: RecoveryStrategy(
        action="retry_fresh",
        delay_seconds=5.0,
        max_retries=1,
        escalate_after=2,
        next_strategy="notify_owner",
    ),
}
```

---

## 4. Class: ErrorClassifier

```python
import random
import re
import time
from typing import Any


class ErrorClassifier:
    """
    에러 분류기.

    Claude Code 세션의 종료 상태를 분석하여 에러 유형을 분류하고,
    적절한 복구 전략을 반환한다. 연속 실패를 추적하여
    에스컬레이션을 관리한다.

    Attributes:
        config: StateManager에서 로드한 Config 딕셔너리
        failure_counts: 에러 유형별 연속 실패 횟수
        _last_classify_result: 마지막 분류 결과 (디버깅용)
    """

    def __init__(self, config: dict[str, Any]) -> None:
        """
        Args:
            config: Config 딕셔너리 (state/config.toml에서 로드)
                필수 키:
                - max_consecutive_failures: int (기본 3)
                - session_timeout_minutes: int (기본 120)
                - backoff_base_seconds: float (기본 1.0)
                - backoff_max_seconds: float (기본 60.0)
                - max_retry_attempts: int (기본 5)
        """
        self.config = config
        self.failure_counts: dict[str, int] = {}
        self._last_classify_result: ErrorType | None = None

    def classify(
        self,
        exit_code: int,
        stderr: str,
        stream_events: list[dict[str, Any]],
    ) -> ErrorType:
        """
        에러를 분류한다.

        분류는 우선순위 순서로 조건을 평가하며,
        첫 번째로 매칭되는 유형을 반환한다.

        Args:
            exit_code: Claude Code 프로세스 종료 코드
            stderr: 표준 에러 출력 전문
            stream_events: stream-json 모드에서 수신한 이벤트 리스트
                이벤트 예시:
                {"type": "api_retry", "retry_after": 30, ...}
                {"type": "assistant", "content": [...], ...}
                {"type": "result", "exit_code": 0, ...}

        Returns:
            분류된 ErrorType
        """
        stderr_lower = stderr.lower()

        # ── 분류 순서 (우선순위 높은 것부터) ──

        # 1. RATE_LIMITED: API rate limit
        if self._is_rate_limited(stderr_lower, stream_events):
            return self._record(ErrorType.RATE_LIMITED)

        # 2. AUTH_FAILURE: 인증/권한 오류
        if self._is_auth_failure(stderr_lower):
            return self._record(ErrorType.AUTH_FAILURE)

        # 3. TRANSIENT_API: 일시적 서버 오류
        if self._is_transient_api(exit_code, stderr_lower):
            return self._record(ErrorType.TRANSIENT_API)

        # 4. NETWORK_ERROR: 네트워크 오류
        if self._is_network_error(stderr_lower):
            return self._record(ErrorType.NETWORK_ERROR)

        # 5. CONTEXT_CORRUPTION: 출력/컨텍스트 손상
        if self._is_context_corruption(stderr_lower, stream_events):
            return self._record(ErrorType.CONTEXT_CORRUPTION)

        # 6. STUCK: 무응답 (타임아웃)
        if self._is_stuck(stream_events):
            return self._record(ErrorType.STUCK)

        # 7. PROCESS_CRASH: 비정상 종료 (위 조건에 해당하지 않는 비정상 종료)
        if self._is_process_crash(exit_code, stream_events):
            return self._record(ErrorType.PROCESS_CRASH)

        # 8. UNKNOWN: 분류 불가
        return self._record(ErrorType.UNKNOWN)

    def _record(self, error_type: ErrorType) -> ErrorType:
        """분류 결과를 기록하고 반환한다."""
        self._last_classify_result = error_type
        return error_type

    # ── 개별 탐지 메서드 ──

    def _is_rate_limited(
        self, stderr_lower: str, stream_events: list[dict[str, Any]]
    ) -> bool:
        """
        Rate limit 탐지.

        탐지 조건:
        - stream_events에 type="api_retry" 이벤트 존재
        - stderr에 "429" 또는 "rate limit" 문자열 존재
        """
        # stream event 기반 탐지
        for event in stream_events:
            if event.get("type") == "api_retry":
                return True

        # stderr 기반 탐지
        if "429" in stderr_lower or "rate limit" in stderr_lower:
            return True

        return False

    def _is_auth_failure(self, stderr_lower: str) -> bool:
        """
        인증/권한 오류 탐지.

        탐지 조건:
        - stderr에 "401", "403" HTTP 상태 코드
        - stderr에 "unauthorized", "forbidden", "auth", "login" 키워드
        """
        auth_patterns = [
            "401",
            "403",
            "unauthorized",
            "forbidden",
            "authentication",
            "auth failed",
            "login required",
            "not authenticated",
            "invalid token",
            "expired token",
        ]
        return any(pattern in stderr_lower for pattern in auth_patterns)

    def _is_transient_api(self, exit_code: int, stderr_lower: str) -> bool:
        """
        일시적 API 서버 오류 탐지.

        탐지 조건:
        - exit_code != 0
        - stderr에 "500", "502", "503", "504", "internal server error" 존재
        """
        if exit_code == 0:
            return False

        transient_patterns = [
            "500",
            "502",
            "503",
            "504",
            "internal server error",
            "bad gateway",
            "service unavailable",
            "gateway timeout",
        ]
        return any(pattern in stderr_lower for pattern in transient_patterns)

    def _is_network_error(self, stderr_lower: str) -> bool:
        """
        네트워크 오류 탐지.

        탐지 조건:
        - stderr에 네트워크 관련 키워드 존재
        """
        network_patterns = [
            "econnrefused",
            "enotfound",
            "econnreset",
            "econnaborted",
            "etimedout",
            "connection refused",
            "connection reset",
            "connection timed out",
            "network error",
            "dns resolution",
            "socket hang up",
            "fetch failed",
        ]
        return any(pattern in stderr_lower for pattern in network_patterns)

    def _is_context_corruption(
        self, stderr_lower: str, stream_events: list[dict[str, Any]]
    ) -> bool:
        """
        컨텍스트/출력 손상 탐지.

        탐지 조건:
        - stderr에 JSON 파싱 에러 관련 메시지
        - stream_events의 마지막 이벤트가 불완전 (type 없음)
        - 비정상적으로 짧은 응답 후 에러
        """
        corruption_patterns = [
            "json parse error",
            "unexpected token",
            "invalid json",
            "malformed",
            "decode error",
            "utf-8",
            "encoding error",
        ]
        if any(pattern in stderr_lower for pattern in corruption_patterns):
            return True

        # 불완전한 stream event 감지
        if stream_events:
            last_event = stream_events[-1]
            if "type" not in last_event:
                return True

        return False

    def _is_stuck(self, stream_events: list[dict[str, Any]]) -> bool:
        """
        무응답(stuck) 탐지.

        탐지 조건:
        - stream_events의 마지막 이벤트 이후 session_timeout_minutes 경과
        - 이 메서드는 Supervisor의 watchdog 타이머가 타임아웃을 감지한 후 호출된다.
          실제 시간 기반 탐지는 Supervisor가 수행하고,
          이 메서드는 타임아웃으로 종료된 세션의 이벤트 패턴을 확인한다.

        참고: 실제로는 Supervisor가 timeout으로 프로세스를 kill한 후
              이 classifier를 호출하므로, stream_events에 타임아웃 마커가 있는지 확인한다.
        """
        # Supervisor가 타임아웃 마커를 삽입한 경우
        for event in stream_events:
            if event.get("type") == "_supervisor_timeout":
                return True

        return False

    def _is_process_crash(
        self, exit_code: int, stream_events: list[dict[str, Any]]
    ) -> bool:
        """
        프로세스 크래시 탐지.

        탐지 조건:
        - exit_code != 0
        - 정상 완료 이벤트 (type="result")가 없음
        """
        if exit_code == 0:
            return False

        # 정상 종료 이벤트 확인
        has_result = any(
            event.get("type") == "result" for event in stream_events
        )
        return not has_result
```

### 분류 결정 트리

```
classify(exit_code, stderr, stream_events)
    │
    ▼
[1. RATE_LIMITED?]
    │  api_retry 이벤트 존재?  ──── 예 → RATE_LIMITED
    │  stderr에 "429"?         ──── 예 → RATE_LIMITED
    │
    ▼ 아니오
[2. AUTH_FAILURE?]
    │  stderr에 "401"/"403"?   ──── 예 → AUTH_FAILURE
    │  stderr에 "auth"/"login"? ─── 예 → AUTH_FAILURE
    │
    ▼ 아니오
[3. TRANSIENT_API?]
    │  exit_code != 0?         ──── 아니오 → 다음
    │  stderr에 "500"/"502"/"503"? ── 예 → TRANSIENT_API
    │
    ▼ 아니오
[4. NETWORK_ERROR?]
    │  stderr에 네트워크 키워드? ─── 예 → NETWORK_ERROR
    │
    ▼ 아니오
[5. CONTEXT_CORRUPTION?]
    │  JSON 파싱 에러?          ──── 예 → CONTEXT_CORRUPTION
    │  불완전 이벤트?           ──── 예 → CONTEXT_CORRUPTION
    │
    ▼ 아니오
[6. STUCK?]
    │  타임아웃 마커 존재?      ──── 예 → STUCK
    │
    ▼ 아니오
[7. PROCESS_CRASH?]
    │  exit_code != 0?         ──── 아니오 → UNKNOWN
    │  result 이벤트 없음?     ──── 예 → PROCESS_CRASH
    │
    ▼ 아니오
[8. UNKNOWN]
```

---

## 5. 복구 전략 선택 및 에스컬레이션

```python
    def get_recovery_strategy(
        self, error_type: ErrorType, attempt: int = 0
    ) -> RecoveryStrategy:
        """
        에러 유형에 맞는 복구 전략을 반환한다.

        연속 실패 횟수에 따라 에스컬레이션이 발생할 수 있다.

        Args:
            error_type: 분류된 에러 유형
            attempt: 현재 재시도 시도 횟수 (0부터 시작)

        Returns:
            적용할 RecoveryStrategy
        """
        base_strategy = RECOVERY_STRATEGIES[error_type]

        # 연속 실패 카운터 확인
        failure_count = self.failure_counts.get(error_type.value, 0)

        # 에스컬레이션 판단
        if failure_count >= base_strategy.escalate_after:
            return self._escalate(base_strategy, error_type)

        # 현재 시도에 대한 delay 계산
        if base_strategy.action in ("retry_resume", "retry_fresh"):
            delay = self.calculate_backoff(
                attempt=attempt,
                base=base_strategy.delay_seconds,
            )
        elif base_strategy.action == "wait_and_resume":
            # rate limit은 retry_after 값을 사용 (호출자가 설정)
            delay = base_strategy.delay_seconds
        else:
            delay = base_strategy.delay_seconds

        return RecoveryStrategy(
            action=base_strategy.action,
            delay_seconds=delay,
            max_retries=base_strategy.max_retries,
            escalate_after=base_strategy.escalate_after,
            next_strategy=base_strategy.next_strategy,
        )

    def _escalate(
        self, current: RecoveryStrategy, error_type: ErrorType
    ) -> RecoveryStrategy:
        """
        에스컬레이션된 복구 전략을 반환한다.

        현재 전략의 max retry를 초과하면 next_strategy로 전환한다.
        next_strategy가 None이면 notify_owner가 최종 전략이 된다.

        Args:
            current: 현재 복구 전략
            error_type: 에러 유형

        Returns:
            에스컬레이션된 RecoveryStrategy
        """
        next_action = current.next_strategy or "notify_owner"

        return RecoveryStrategy(
            action=next_action,
            delay_seconds=0.0,
            max_retries=0,
            escalate_after=0,
            next_strategy=None,
        )
```

### 에스컬레이션 흐름

```
[에러 발생] → classify() → TRANSIENT_API
    │
    ▼
get_recovery_strategy(TRANSIENT_API, attempt=0)
    │  failure_count: 0 < escalate_after: 5
    │  → action: "retry_resume", delay: 1.0s
    │
    ▼
[재시도 실패] → classify() → TRANSIENT_API
    │
    ▼
record_failure(TRANSIENT_API)  → failure_count: 1
get_recovery_strategy(TRANSIENT_API, attempt=1)
    │  failure_count: 1 < escalate_after: 5
    │  → action: "retry_resume", delay: 2.0s (백오프)
    │
    ▼
[재시도 실패 반복...]
    │
    ▼
record_failure(TRANSIENT_API)  → failure_count: 5
get_recovery_strategy(TRANSIENT_API, attempt=5)
    │  failure_count: 5 >= escalate_after: 5
    │  → _escalate() → action: "notify_owner"
    │
    ▼
[Supervisor가 Owner에 Slack 알림 전송]
    │  "API 서버 에러가 5회 연속 발생했습니다. 확인이 필요합니다."
    │
    ▼
[Owner 응답 또는 외부 조건 변경 대기]
```

---

## 6. 백오프 계산기 (Backoff Calculator)

```python
    def calculate_backoff(
        self,
        attempt: int,
        base: float | None = None,
        max_delay: float | None = None,
    ) -> float:
        """
        지수 백오프 + 지터를 계산한다.

        공식: delay = min(base * 2^attempt, max_delay) + jitter
        지터: 0 ~ 25% 랜덤 추가 (thundering herd 방지)

        Args:
            attempt: 시도 횟수 (0부터 시작)
            base: 기본 대기 시간 (초). None이면 config에서 로드.
            max_delay: 최대 대기 시간 (초). None이면 config에서 로드.

        Returns:
            대기 시간 (초)

        Examples:
            >>> classifier.calculate_backoff(attempt=0, base=1.0)
            1.0 ~ 1.25  (base + 0~25% jitter)
            >>> classifier.calculate_backoff(attempt=1, base=1.0)
            2.0 ~ 2.5
            >>> classifier.calculate_backoff(attempt=2, base=1.0)
            4.0 ~ 5.0
            >>> classifier.calculate_backoff(attempt=3, base=1.0)
            8.0 ~ 10.0
            >>> classifier.calculate_backoff(attempt=10, base=1.0, max_delay=60.0)
            60.0 ~ 75.0  (capped at max_delay + jitter)
        """
        if base is None:
            base = self.config.get("backoff_base_seconds", 1.0)
        if max_delay is None:
            max_delay = self.config.get("backoff_max_seconds", 60.0)

        # 지수 백오프: base * 2^attempt
        delay = base * (2 ** attempt)

        # 최대값 제한
        delay = min(delay, max_delay)

        # 지터: 0~25% 랜덤 추가
        jitter = delay * random.uniform(0.0, 0.25)
        delay += jitter

        return delay

    @staticmethod
    def extract_retry_after(stream_events: list[dict[str, Any]]) -> float | None:
        """
        Rate limit 이벤트에서 retry_after 값을 추출한다.

        Args:
            stream_events: stream-json 이벤트 리스트

        Returns:
            대기 시간 (초). 값이 없으면 None.
        """
        for event in reversed(stream_events):
            if event.get("type") == "api_retry":
                retry_after = event.get("retry_after")
                if retry_after is not None:
                    return float(retry_after)
        return None
```

### 백오프 시각화

```
attempt │ base=1s delay (지터 제외)  │  base=2s delay (지터 제외)
────────┼──────────────────────────────┼──────────────────────────────
   0    │  1.0s                        │  2.0s
   1    │  2.0s                        │  4.0s
   2    │  4.0s                        │  8.0s
   3    │  8.0s                        │  16.0s
   4    │  16.0s                       │  32.0s
   5    │  32.0s                       │  60.0s (max capped)
   6    │  60.0s (max capped)          │  60.0s (max capped)

지터 범위: 각 delay의 0~25% 추가
예: delay=8.0s → 실제 8.0~10.0s
```

---

## 7. 연속 실패 추적 (Consecutive Failure Tracking)

```python
    def record_failure(self, error_type: ErrorType) -> int:
        """
        에러 유형의 연속 실패를 기록한다.

        Args:
            error_type: 실패한 에러 유형

        Returns:
            현재 연속 실패 횟수
        """
        key = error_type.value
        self.failure_counts[key] = self.failure_counts.get(key, 0) + 1
        return self.failure_counts[key]

    def record_success(self, error_type: ErrorType | None = None) -> None:
        """
        성공을 기록하여 연속 실패 카운터를 리셋한다.

        Args:
            error_type: 특정 에러 유형만 리셋. None이면 모든 카운터 리셋.
        """
        if error_type is None:
            self.failure_counts.clear()
        else:
            self.failure_counts.pop(error_type.value, None)

    def get_failure_count(self, error_type: ErrorType) -> int:
        """특정 에러 유형의 현재 연속 실패 횟수를 반환한다."""
        return self.failure_counts.get(error_type.value, 0)

    def should_escalate(self, error_type: ErrorType) -> bool:
        """
        에스컬레이션이 필요한지 판단한다.

        현재 연속 실패 횟수가 해당 에러 유형의 escalate_after를 초과하면 True.

        Args:
            error_type: 에러 유형

        Returns:
            에스컬레이션 필요 여부
        """
        strategy = RECOVERY_STRATEGIES[error_type]
        count = self.get_failure_count(error_type)
        return count >= strategy.escalate_after

    def should_notify_owner(self) -> bool:
        """
        어떤 에러 유형이든 max_consecutive_failures를 초과했는지 확인한다.

        config.toml의 max_consecutive_failures (기본 3)와 비교하여
        전체 시스템 수준의 알림 판단에 사용한다.

        Returns:
            Owner 알림 필요 여부
        """
        max_failures = self.config.get("max_consecutive_failures", 3)
        total_consecutive = sum(self.failure_counts.values())
        return total_consecutive >= max_failures
```

### 연속 실패 추적 상태 다이어그램

```
[정상 운영]
    │
    ├── 세션 성공
    │   └── record_success() → 모든 카운터 리셋 → [정상 운영]
    │
    └── 세션 실패
        │
        ▼
    classify() → error_type
    record_failure(error_type) → count += 1
        │
        ├── count < escalate_after
        │   └── get_recovery_strategy() → 기본 전략 반환 → 재시도
        │       │
        │       ├── 재시도 성공 → record_success() → [정상 운영]
        │       └── 재시도 실패 → [세션 실패] (루프)
        │
        └── count >= escalate_after
            └── get_recovery_strategy() → 에스컬레이션
                │
                ├── next_strategy != None
                │   └── 에스컬레이션된 전략 실행
                │
                └── next_strategy == None (또는 "notify_owner")
                    └── Owner 알림 전송 → [대기]
                        │
                        ├── Owner 응답/해결 → record_success() → [정상 운영]
                        └── 타임아웃 → 계속 대기
```

---

## 8. 통합 인터페이스: classify_and_recover()

Supervisor가 단일 호출로 분류와 복구 전략을 함께 얻는 편의 메서드.

```python
    def classify_and_recover(
        self,
        exit_code: int,
        stderr: str,
        stream_events: list[dict[str, Any]],
        attempt: int = 0,
    ) -> tuple[ErrorType, RecoveryStrategy]:
        """
        에러를 분류하고 복구 전략을 반환한다.

        분류 → 실패 기록 → 복구 전략 선택을 한 번에 수행한다.
        Rate limit의 경우 retry_after 값을 자동으로 반영한다.

        Args:
            exit_code: 프로세스 종료 코드
            stderr: 표준 에러 출력
            stream_events: stream-json 이벤트 리스트
            attempt: 현재 재시도 횟수

        Returns:
            (에러 유형, 복구 전략) 튜플

        Usage:
            error_type, strategy = classifier.classify_and_recover(
                exit_code=1,
                stderr="502 Bad Gateway",
                stream_events=[...],
                attempt=2,
            )

            if strategy.action == "retry_resume":
                time.sleep(strategy.delay_seconds)
                session_manager.resume_session()
            elif strategy.action == "notify_owner":
                slack_client.notify_error(error_type, strategy)
        """
        # 1. 분류
        error_type = self.classify(exit_code, stderr, stream_events)

        # 2. 실패 기록
        failure_count = self.record_failure(error_type)

        # 3. 복구 전략 선택
        strategy = self.get_recovery_strategy(error_type, attempt)

        # 4. Rate limit 특수 처리: retry_after 값 반영
        if error_type == ErrorType.RATE_LIMITED:
            retry_after = self.extract_retry_after(stream_events)
            if retry_after is not None:
                strategy = RecoveryStrategy(
                    action=strategy.action,
                    delay_seconds=retry_after,
                    max_retries=strategy.max_retries,
                    escalate_after=strategy.escalate_after,
                    next_strategy=strategy.next_strategy,
                )

        return error_type, strategy
```

---

## 9. Supervisor 통합 흐름

Supervisor의 세션 루프에서 ErrorClassifier가 사용되는 전체 흐름.

```
[Supervisor 세션 루프]
    │
    ▼
[Claude Code 세션 실행]
    │
    ├── exit_code == 0 + result 이벤트 존재
    │   └── 정상 종료
    │       classifier.record_success()
    │       → 다음 미션
    │
    └── 비정상 종료 (exit_code != 0 또는 타임아웃)
        │
        ▼
    error_type, strategy = classifier.classify_and_recover(
        exit_code, stderr, stream_events, attempt
    )
        │
        ▼
    [Friction 기록]
        state_manager.add_friction({
            "type": "error",
            "pattern_key": error_type.value,
            "description": f"{error_type.value} 에러 발생",
            "context": {"exit_code": exit_code, "attempt": attempt},
        })
        │
        ▼
    match strategy.action:
        │
        ├── "retry_resume"
        │   └── time.sleep(strategy.delay_seconds)
        │       session_manager.resume_session(session_id)
        │       attempt += 1
        │
        ├── "retry_fresh"
        │   └── time.sleep(strategy.delay_seconds)
        │       state_manager.recover_from_crash()
        │       → 새 세션 시작 (루프 처음으로)
        │
        ├── "wait_and_resume"
        │   └── time.sleep(strategy.delay_seconds)  # retry_after 값
        │       session_manager.resume_session(session_id)
        │
        ├── "checkpoint_restore"
        │   └── checkpoints = state_manager.list_checkpoints()
        │       state_manager.restore_checkpoint(checkpoints[0])
        │       → 새 세션 시작 (루프 처음으로)
        │
        └── "notify_owner"
            └── slack_client.send_error_notification(
                    error_type, failure_count, stderr_excerpt
                )
                → Owner 응답 대기
                → record_success() 후 루프 재개
```

---

## 10. 설정 연동

ErrorClassifier의 동작은 `state/config.toml`의 값에 의해 조정된다. Claude Code 세션이 자기개선(S-5)의 일환으로 이 값들을 수정할 수 있다.

| 설정 키 | 기본값 | 설명 |
|---------|--------|------|
| `max_consecutive_failures` | 3 | 전체 연속 실패 임계값 (초과 시 Owner 알림) |
| `max_retry_attempts` | 5 | 전략별 최대 재시도 (RECOVERY_STRATEGIES의 max_retries 기본값) |
| `backoff_base_seconds` | 1.0 | 지수 백오프 base 값 |
| `backoff_max_seconds` | 60.0 | 지수 백오프 최대값 |
| `session_timeout_minutes` | 120 | STUCK 판정 타임아웃 |

### 자기개선 시나리오

Claude Code가 Friction 분석을 통해 다음과 같은 설정 변경을 수행할 수 있다:

```
[Friction 분석]
  "TRANSIENT_API 에러가 빈번하지만 3회 이내에 항상 복구됨"
      │
      ▼
[config.toml 수정]
  backoff_base_seconds: 1.0 → 2.0    (더 여유있게 대기)
  max_retry_attempts: 5 → 8           (더 많이 재시도)

[Friction 분석]
  "NETWORK_ERROR가 10회 재시도 전에 항상 복구됨 — 불필요한 Owner 알림 발생"
      │
      ▼
[config.toml 수정]
  max_consecutive_failures: 3 → 5     (에스컬레이션 임계값 상향)
```

---

## 11. 에러 처리 정책

| 상황 | 처리 |
|------|------|
| stderr가 빈 문자열 | 패턴 매칭 실패 → PROCESS_CRASH 또는 UNKNOWN으로 분류 |
| stream_events가 빈 리스트 | 이벤트 기반 탐지 건너뜀, stderr/exit_code만으로 분류 |
| 복수 에러 유형 매칭 | 우선순위 순서의 첫 번째 매칭을 사용 (rate limit > auth > transient > ...) |
| classify 중 예외 | UNKNOWN 반환 (분류기 자체는 절대 예외를 전파하지 않아야 함) |
| failure_counts 영속화 | 메모리에만 유지. Supervisor 재시작 시 리셋. (의도: 재시작 자체가 복구 행위) |
