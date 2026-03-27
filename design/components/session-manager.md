# Session Manager 컴포넌트 설계

> **파일**: `system/session_manager.py`
> **역할**: Claude Code 프로세스의 생명주기를 관리한다. 시작, 모니터링, 종료, 재개를 담당한다.

---

## 1. 개요

SessionManager는 Claude Code CLI 프로세스를 직접 제어하는 컴포넌트이다. Supervisor가 "이 프롬프트로 세션을 실행하라"고 지시하면, SessionManager가 실제 프로세스를 생성하고, stream-json 출력을 실시간 파싱하여 이벤트 스트림으로 변환하고, 종료 시 정리를 수행한다.

### 핵심 원칙

- **프로세스 격리**: 각 Claude Code 세션은 독립 프로세스 그룹(session)에서 실행된다. 종료 시 자식 프로세스까지 깨끗하게 정리할 수 있다.
- **실시간 관찰**: stream-json 이벤트를 한 줄씩 파싱하여 Supervisor에 AsyncIterator로 전달한다. 지연 없는 실시간 모니터링이 가능하다.
- **결정론적 명령 구성**: 세션 시작 명령은 항상 동일한 패턴으로 구성된다. 환경 변수, 플래그, 프롬프트가 코드에 명시되어 있다.

---

## 2. 데이터 타입

### 2.1 세션 정보

```python
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, AsyncIterator, Optional


class SessionState(Enum):
    """세션 생명주기 상태."""
    LAUNCHING = "launching"         # 프로세스 시작 중
    RUNNING = "running"             # 정상 실행 중
    RATE_LIMITED = "rate_limited"   # rate limit 대기 중 (자동 재시도)
    STOPPING = "stopping"           # 종료 진행 중 (SIGTERM 전송됨)
    COMPLETED = "completed"         # 정상 종료
    CRASHED = "crashed"             # 비정상 종료
    TIMED_OUT = "timed_out"         # 타임아웃으로 강제 종료


@dataclass
class SessionInfo:
    """실행 중인 또는 완료된 세션의 정보."""
    session_id: str = ""                      # Claude Code가 반환한 세션 ID
    process: Optional[asyncio.subprocess.Process] = None
    state: SessionState = SessionState.LAUNCHING
    prompt: str = ""                          # 세션 시작 프롬프트
    mission_id: str = ""                      # 연결된 미션 ID

    # 타이밍
    started_at: float = field(default_factory=time.time)
    ended_at: Optional[float] = None

    # 결과
    exit_code: Optional[int] = None
    result_text: str = ""
    is_error: bool = False
    cost_usd: float = 0.0
    num_turns: int = 0
    duration_ms: int = 0

    # 모니터링용 버퍼
    stderr_buffer: str = ""
    last_event: Optional[StreamEvent] = None

    # 통계
    event_count: int = 0
    tool_use_count: int = 0
    api_retry_count: int = 0
    compaction_count: int = 0


@dataclass
class SessionStatus:
    """현재 세션 상태 요약. TUI, Slack, 외부 조회용."""
    is_active: bool = False
    session_id: str = ""
    state: SessionState = SessionState.COMPLETED
    mission_id: str = ""
    running_seconds: float = 0.0
    event_count: int = 0
    tool_use_count: int = 0
    api_retry_count: int = 0
    last_event_type: str = ""
    last_event_time: float = 0.0
```

### 2.2 스트림 이벤트

```python
@dataclass
class StreamEvent:
    """
    stream-json에서 파싱한 단일 이벤트.

    Claude Code의 --output-format stream-json은
    줄바꿈으로 구분된 JSON 객체를 stdout으로 출력한다.
    각 줄이 하나의 StreamEvent로 변환된다.
    """
    type: str = ""              # "system", "assistant", "result"
    subtype: str = ""           # "init", "api_retry", "text", "tool_use", "tool_result"
    data: dict[str, Any] = field(default_factory=dict)  # 원본 JSON 전체
    timestamp: float = field(default_factory=time.time)

    # 편의 프로퍼티
    @property
    def session_id(self) -> str:
        """system/init 이벤트에서 session_id를 추출한다."""
        return self.data.get("session_id", "")

    @property
    def is_rate_limit(self) -> bool:
        """api_retry 이벤트가 rate_limit인지 확인한다."""
        return (
            self.type == "system"
            and self.subtype == "api_retry"
            and self.data.get("error") == "rate_limit"
        )

    @property
    def retry_delay_ms(self) -> int:
        """api_retry 이벤트의 재시도 대기 시간(ms)."""
        return self.data.get("retry_delay_ms", 0)

    @property
    def is_result(self) -> bool:
        """세션 최종 결과 이벤트인지 확인한다."""
        return self.type == "result"

    @property
    def result_text(self) -> str:
        """result 이벤트의 텍스트를 추출한다."""
        return self.data.get("result", "")

    @property
    def is_error(self) -> bool:
        """result 이벤트가 에러인지 확인한다."""
        return self.data.get("is_error", False)

    @property
    def is_tool_use(self) -> bool:
        """도구 호출 이벤트인지 확인한다."""
        return self.subtype == "tool_use"

    @property
    def is_tool_result(self) -> bool:
        """도구 결과 이벤트인지 확인한다."""
        return self.subtype == "tool_result"

    @property
    def tool_name(self) -> str:
        """도구 호출/결과의 도구 이름."""
        if self.is_tool_use:
            return self.data.get("tool", {}).get("name", "")
        return ""
```

---

## 3. Class: SessionManager

### 3.1 클래스 인터페이스

```python
import json
import logging
import os
import signal
from pathlib import Path
from typing import AsyncIterator, Optional


class SessionManager:
    """
    Claude Code 프로세스의 생명주기를 관리한다.

    Supervisor가 이 클래스의 메서드를 호출하여 세션을 시작, 모니터링, 종료한다.
    하나의 SessionManager 인스턴스는 동시에 하나의 세션만 관리한다.
    """

    def __init__(self, project_root: Path) -> None: ...

    # ── 세션 시작 ─────────────────────────────────────────────

    async def launch_session(
        self,
        prompt: str,
        *,
        mission_id: str = "",
    ) -> SessionInfo:
        """
        새 Claude Code 세션을 시작한다.

        Args:
            prompt: 세션 프롬프트. state_manager가 생성한 컨텍스트 포함.
            mission_id: 이 세션이 실행할 미션의 ID.

        Returns:
            SessionInfo: 시작된 세션의 정보. process 필드에 프로세스 핸들.

        Raises:
            RuntimeError: 이미 활성 세션이 있는 경우.
            OSError: 프로세스 시작 실패.
        """
        ...

    async def resume_session(
        self,
        session_id: str,
        prompt: str,
        *,
        mission_id: str = "",
    ) -> SessionInfo:
        """
        기존 세션을 --resume으로 재개한다.

        rate limit 복구 또는 의도적 재개 시 사용한다.
        이전 세션의 대화 이력이 복원된다.

        Args:
            session_id: 재개할 세션의 ID.
            prompt: 재개 시 전달할 추가 프롬프트.
            mission_id: 연결된 미션 ID.

        Returns:
            SessionInfo: 재개된 세션의 정보.
        """
        ...

    # ── 모니터링 ──────────────────────────────────────────────

    async def monitor_session(
        self,
        proc: asyncio.subprocess.Process,
    ) -> AsyncIterator[StreamEvent]:
        """
        실행 중인 세션의 stream-json 출력을 파싱하여 이벤트 스트림으로 제공한다.

        stdout에서 줄 단위로 JSON을 읽어 StreamEvent로 변환한다.
        프로세스가 종료되면 이터레이터도 종료된다.

        Args:
            proc: asyncio 서브프로세스 핸들.

        Yields:
            StreamEvent: 파싱된 이벤트. system/init, assistant, result 등.

        Note:
            이 메서드는 async generator이다. `async for event in monitor_session(proc):`
            패턴으로 사용한다. 프로세스 종료 시 자연스럽게 루프가 끝난다.
        """
        ...

    # ── 종료 ──────────────────────────────────────────────────

    async def terminate_session(
        self,
        session_id: str,
        *,
        graceful: bool = True,
    ) -> None:
        """
        실행 중인 세션을 종료한다.

        graceful=True: SIGTERM → 10초 대기 → SIGKILL (프로세스 그룹 전체)
        graceful=False: 즉시 SIGKILL (프로세스 그룹 전체)

        Args:
            session_id: 종료할 세션의 ID.
            graceful: True이면 SIGTERM으로 우아한 종료를 시도.
        """
        ...

    # ── 상태 조회 ─────────────────────────────────────────────

    def get_session_status(self) -> SessionStatus:
        """
        현재 세션 상태를 반환한다.

        활성 세션이 없으면 is_active=False인 SessionStatus를 반환한다.
        TUI, Slack 상태 보고, 외부 조회에 사용한다.

        Returns:
            SessionStatus: 현재 세션의 요약 정보.
        """
        ...
```

### 3.2 내부 속성

```python
def __init__(self, project_root: Path) -> None:
    self.project_root = project_root
    self.log = logging.getLogger("session_manager")

    # 현재 활성 세션
    self._current: Optional[SessionInfo] = None

    # Claude Code CLI 경로 (PATH에서 탐색)
    self._claude_bin: str = "claude"

    # 세션 환경 변수 (프로세스에 전달)
    self._session_env: dict[str, str] = self._build_session_env()

    # 타임아웃 설정 (config에서 로드 가능)
    self.session_timeout_s: float = 0  # 0 = 무제한
    self.graceful_shutdown_timeout_s: float = 10.0
```

---

## 4. 세션 시작 명령 구성

### 4.1 기본 명령

```bash
claude -p "<prompt>" \
  --dangerously-skip-permissions \
  --model opus \
  --effort max \
  --output-format stream-json
```

### 4.2 명령 구성 로직

```python
def _build_launch_command(
    self,
    prompt: str,
    *,
    resume_session_id: Optional[str] = None,
) -> list[str]:
    """
    Claude Code CLI 실행 명령을 구성한다.

    Args:
        prompt: 세션 프롬프트.
        resume_session_id: 재개할 세션 ID. None이면 새 세션.

    Returns:
        list[str]: subprocess에 전달할 명령 배열.
    """
    cmd = [
        self._claude_bin,
        "-p", prompt,
        "--dangerously-skip-permissions",
        "--model", "opus",
        "--effort", "max",
        "--output-format", "stream-json",
        # 격리 (E-1) — 상세: encapsulation.md
        "--setting-sources", "project,local",  # Tier 2: User settings 차단
        "--strict-mcp-config",                 # Tier 1: User/Cloud MCP 차단
        "--mcp-config", "{}",                  # 빈 MCP 설정 (필요 시 파일 경로로 교체)
    ]

    # 세션 재개
    if resume_session_id is not None:
        cmd.extend(["--resume", resume_session_id])

    return cmd
```

### 4.3 환경 변수

상세: [encapsulation.md](../encapsulation.md)

Claude Code 프로세스에는 `os.environ`을 상속하되, 핵심 동작을 제어하는 변수를 명시적으로 덮어쓰고, 오염 변수를 제거한다.

`os.environ`을 상속하지 않는 "깨끗한 환경" 방식은 채택하지 않는다 — Claude Code(Node.js)가 예측하지 못한 환경 변수에 의존할 위험이 크다.

```python
def _build_session_env(self) -> dict[str, str]:
    """
    Claude Code 프로세스에 전달할 환경 변수를 구성한다.

    os.environ을 기반으로, 핵심 동작 변수를 덮어쓰고 오염 변수를 제거한다.
    """
    env = os.environ.copy()

    # ── Tier 1: 핵심 동작 고정 (공식 문서에 우선순위 명시됨) ──
    env["CLAUDE_CODE_EFFORT_LEVEL"] = "max"           # /config 변경 무시
    env["CLAUDE_CODE_SUBAGENT_MODEL"] = "opus"         # 서브에이전트 모델 고정
    env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"       # User auto-memory 차단
    env["DISABLE_AUTOUPDATER"] = "1"                   # 자율 운영 중 자동 업데이트 방지

    # ── 오염 변수 제거 (기본값 복원) ──
    env.pop("ANTHROPIC_API_KEY", None)            # Claude Max OAuth 강제 (D-6)
    env.pop("ANTHROPIC_MODEL", None)              # --model opus가 우선하지만 이중 보장
    env.pop("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", None)  # 기본값(95%) 사용 — 컨텍스트 최대 활용

    # ── Python ──
    env["PYTHONUNBUFFERED"] = "1"

    return env
```

| 변수 | 값 | Tier | 공식 문서 근거 |
|------|-----|------|-------------|
| `CLAUDE_CODE_EFFORT_LEVEL` | `max` | 1 | "Takes precedence over /effort and the effortLevel setting" |
| `CLAUDE_CODE_SUBAGENT_MODEL` | `opus` | 1 | Env Vars 문서 |
| `CLAUDE_CODE_DISABLE_AUTO_MEMORY` | `1` | 1 | Env Vars 문서 |
| `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` | **(제거)** | 1 | 기본값 95% 사용 |
| `ANTHROPIC_API_KEY` | **(제거)** | 1 | 설정 시 Claude Max 대신 API 과금 |
| `ANTHROPIC_MODEL` | **(제거)** | 1 | --model 플래그로 충분하나 이중 보장 |

### 4.4 프로세스 그룹

`start_new_session=True`로 새 프로세스 그룹(세션)을 생성한다. 이렇게 하면 종료 시 `os.killpg`로 Claude Code와 그 자식 프로세스(서브에이전트 등)를 한 번에 정리할 수 있다.

```python
async def launch_session(
    self,
    prompt: str,
    *,
    mission_id: str = "",
) -> SessionInfo:
    """새 Claude Code 세션을 시작한다."""
    if self._current is not None and self._current.state == SessionState.RUNNING:
        raise RuntimeError(
            f"이미 활성 세션 존재: {self._current.session_id}"
        )

    cmd = self._build_launch_command(prompt)
    self.log.info("세션 시작: %s", " ".join(cmd[:6]) + " ...")

    # 프로세스 생성
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(self.project_root),
        env=self._session_env,
        start_new_session=True,  # 새 프로세스 그룹 생성
    )

    info = SessionInfo(
        process=proc,
        state=SessionState.LAUNCHING,
        prompt=prompt,
        mission_id=mission_id,
    )
    self._current = info

    self.log.info("프로세스 시작됨: pid=%d", proc.pid)
    return info


async def resume_session(
    self,
    session_id: str,
    prompt: str,
    *,
    mission_id: str = "",
) -> SessionInfo:
    """기존 세션을 --resume으로 재개한다."""
    cmd = self._build_launch_command(
        prompt,
        resume_session_id=session_id,
    )
    self.log.info("세션 재개: session_id=%s", session_id)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(self.project_root),
        env=self._session_env,
        start_new_session=True,
    )

    info = SessionInfo(
        session_id=session_id,
        process=proc,
        state=SessionState.LAUNCHING,
        prompt=prompt,
        mission_id=mission_id,
    )
    self._current = info

    self.log.info("세션 재개 프로세스 시작됨: pid=%d", proc.pid)
    return info
```

---

## 5. Stream-JSON 이벤트 파싱

### 5.1 이벤트 타입 일람

Claude Code `--output-format stream-json`이 출력하는 줄바꿈 구분 JSON 이벤트의 종류이다.

| type | subtype | 의미 | 핵심 필드 |
|------|---------|------|-----------|
| `system` | `init` | 세션 초기화 완료 | `session_id` |
| `system` | `api_retry` | API 재시도 (rate limit 등) | `error`, `attempt`, `max_retries`, `retry_delay_ms`, `error_status` |
| `assistant` | (text content) | Claude의 텍스트 응답 | `content[].text` |
| `assistant` | (tool_use) | 도구 호출 시작 | `content[].type="tool_use"`, `content[].name`, `content[].input` |
| `assistant` | (tool_result) | 도구 실행 결과 | `content[].type="tool_result"`, `content[].content` |
| `result` | `success` | 세션 정상 완료 | `result`, `session_id`, `total_cost_usd`, `duration_ms`, `num_turns`, `usage` |
| `result` | `error` | 세션 에러 종료 | `result`, `is_error=true`, `session_id` |

### 5.2 이벤트 감지 매핑

| 감지 대상 | 감지 방법 |
|-----------|-----------|
| **세션 시작** | `type == "system" && subtype == "init"` → `session_id` 추출 |
| **정상 완료** | `type == "result" && subtype == "success"` |
| **에러 종료** | `type == "result" && is_error == true` |
| **Rate Limit** | `type == "system" && subtype == "api_retry" && error == "rate_limit"` |
| **서버 에러** | `type == "system" && subtype == "api_retry" && error == "server_error"` |
| **인증 실패** | `type == "system" && subtype == "api_retry" && error == "authentication_failed"` |
| **Compaction** | `type == "system" && subtype == "compact"` (존재 시) 또는 토큰 사용량 급감 |
| **도구 호출** | assistant 이벤트 내 `content[].type == "tool_use"` |
| **도구 결과** | assistant 이벤트 내 `content[].type == "tool_result"` |

### 5.3 파싱 구현

```python
async def monitor_session(
    self,
    proc: asyncio.subprocess.Process,
) -> AsyncIterator[StreamEvent]:
    """
    stream-json 출력을 실시간으로 파싱하여 StreamEvent를 yield한다.

    stdout에서 한 줄씩 읽어 JSON 파싱을 시도한다.
    파싱 실패한 줄은 경고 로그만 남기고 건너뛴다.
    프로세스 종료 시 이터레이터도 종료된다.
    """
    assert proc.stdout is not None, "stdout이 PIPE로 설정되어야 합니다"

    info = self._current
    if info is not None:
        info.state = SessionState.RUNNING

    # stderr를 별도로 수집하는 백그라운드 태스크
    stderr_task = asyncio.create_task(
        self._collect_stderr(proc),
        name="stderr-collector",
    )

    try:
        while True:
            # 한 줄 읽기 (타임아웃 포함)
            try:
                if self.session_timeout_s > 0:
                    line_bytes = await asyncio.wait_for(
                        proc.stdout.readline(),
                        timeout=self.session_timeout_s,
                    )
                else:
                    line_bytes = await proc.stdout.readline()
            except asyncio.TimeoutError:
                self.log.warning("세션 타임아웃: %s초", self.session_timeout_s)
                if info is not None:
                    info.state = SessionState.TIMED_OUT
                await self._force_terminate(proc)
                return

            # EOF — 프로세스 종료
            if not line_bytes:
                break

            line = line_bytes.decode("utf-8", errors="replace").strip()
            if not line:
                continue

            # JSON 파싱
            event = self._parse_stream_line(line)
            if event is None:
                continue

            # 내부 상태 갱신
            self._update_session_info(info, event)

            # Supervisor에 이벤트 전달
            yield event

    finally:
        # 프로세스 종료 대기
        await proc.wait()
        stderr_task.cancel()

        # 최종 상태 갱신
        if info is not None:
            info.exit_code = proc.returncode
            info.ended_at = time.time()
            if info.state == SessionState.RUNNING:
                if proc.returncode == 0:
                    info.state = SessionState.COMPLETED
                else:
                    info.state = SessionState.CRASHED

        self.log.info(
            "세션 종료: pid=%d exit_code=%s state=%s",
            proc.pid,
            proc.returncode,
            info.state.value if info else "unknown",
        )


def _parse_stream_line(self, line: str) -> Optional[StreamEvent]:
    """
    한 줄의 stream-json 출력을 StreamEvent로 파싱한다.

    JSON 파싱 실패 시 None을 반환한다.
    """
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        self.log.debug("JSON 파싱 실패 (무시): %s", line[:200])
        return None

    event_type = data.get("type", "")
    subtype = data.get("subtype", "")

    # assistant 이벤트의 subtype 결정
    # assistant 이벤트는 subtype 필드가 없을 수 있다.
    # content 배열의 첫 번째 항목 type으로 판단한다.
    if event_type == "assistant" and not subtype:
        content = data.get("content", [])
        if content and isinstance(content, list):
            first_content_type = content[0].get("type", "")
            if first_content_type in ("tool_use", "tool_result"):
                subtype = first_content_type
            else:
                subtype = "text"

    return StreamEvent(
        type=event_type,
        subtype=subtype,
        data=data,
    )


def _update_session_info(
    self,
    info: Optional[SessionInfo],
    event: StreamEvent,
) -> None:
    """세션 정보를 이벤트 데이터로 갱신한다."""
    if info is None:
        return

    info.last_event = event
    info.event_count += 1

    match event.type:
        case "system" if event.subtype == "init":
            info.session_id = event.session_id
            info.state = SessionState.RUNNING

        case "system" if event.subtype == "api_retry":
            info.api_retry_count += 1
            if event.is_rate_limit:
                info.state = SessionState.RATE_LIMITED

        case "assistant" if event.is_tool_use:
            info.tool_use_count += 1

        case "result":
            info.result_text = event.result_text
            info.is_error = event.is_error
            info.cost_usd = event.data.get("total_cost_usd", 0.0)
            info.num_turns = event.data.get("num_turns", 0)
            info.duration_ms = event.data.get("duration_ms", 0)
            if event.is_error:
                info.state = SessionState.CRASHED
            else:
                info.state = SessionState.COMPLETED


async def _collect_stderr(
    self,
    proc: asyncio.subprocess.Process,
) -> None:
    """stderr를 백그라운드로 수집한다. 에러 분류에 사용."""
    assert proc.stderr is not None
    try:
        buffer_parts: list[str] = []
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace")
            buffer_parts.append(text)
            self.log.debug("STDERR> %s", text.strip())

        if self._current is not None:
            self._current.stderr_buffer = "".join(buffer_parts)
    except asyncio.CancelledError:
        pass
```

---

## 6. 세션 생명주기 상태 다이어그램

```
                    launch_session()
                          │
                          ▼
                  ┌───────────────┐
                  │   LAUNCHING   │
                  │               │
                  │ 프로세스 시작  │
                  │ stdout 대기   │
                  └───────┬───────┘
                          │
                    system/init 수신
                          │
                          ▼
               ┌──────────────────┐
        ┌─────▶│     RUNNING     │◀────────┐
        │      │                  │          │
        │      │ stream-json     │          │
        │      │ 이벤트 수신 중   │          │
        │      └───┬────┬────┬───┘          │
        │          │    │    │              │
        │          │    │    │              │
        │          │    │    └──────────────┤
        │          │    │    api_retry      │
        │          │    │    (rate_limit)   │
        │          │    │         │         │
        │          │    │         ▼         │
        │          │    │  ┌──────────────┐ │
        │          │    │  │ RATE_LIMITED │ │
        │          │    │  │              │ │
        │          │    │  │ 자동 재시도  │ │
        │          │    │  │ 대기 중      │──┘
        │          │    │  └──────────────┘
        │          │    │         retry 성공 시 RUNNING으로 복귀
        │          │    │
        │          │    │ terminate_session(graceful=True)
        │          │    │         │
        │          │    │         ▼
        │          │    │  ┌──────────────┐
        │          │    │  │   STOPPING   │
        │          │    │  │              │
        │          │    │  │ SIGTERM 전송 │
        │          │    │  │ 10초 대기    │
        │          │    │  │ SIGKILL      │
        │          │    │  └──────┬───────┘
        │          │    │         │
        │          │    │         ▼
        │          │    │  ┌──────────────┐
        │          │    │  │  COMPLETED   │  (exit_code=0 또는 SIGTERM)
        │          │    │  └──────────────┘
        │          │    │
        │          │    │ result (success)
        │          │    └──────▶ COMPLETED
        │          │
        │          │ result (error) 또는 비정상 exit
        │          └──────────▶ ┌──────────────┐
        │                      │   CRASHED    │
        │                      └──────────────┘
        │
        │ session_timeout 초과
        │          │
        │          ▼
        │   ┌──────────────┐
        │   │  TIMED_OUT   │
        │   │              │
        │   │ SIGTERM→KILL │
        │   └──────────────┘
        │
        │ resume_session()
        └──── (RATE_LIMITED 또는 CRASHED 상태에서)
```

### 상태 전이 표

| 현재 상태 | 트리거 | 다음 상태 | 설명 |
|-----------|--------|-----------|------|
| `LAUNCHING` | system/init 이벤트 수신 | `RUNNING` | 세션 초기화 완료, session_id 확보 |
| `RUNNING` | result (success) | `COMPLETED` | 정상 종료 |
| `RUNNING` | result (error) | `CRASHED` | 에러로 종료 |
| `RUNNING` | 프로세스 비정상 종료 (exit != 0) | `CRASHED` | 크래시 |
| `RUNNING` | api_retry (rate_limit) | `RATE_LIMITED` | rate limit 감지 |
| `RUNNING` | terminate_session() 호출 | `STOPPING` | 외부에서 종료 요청 |
| `RUNNING` | 타임아웃 초과 | `TIMED_OUT` | 세션 시간 초과 |
| `RATE_LIMITED` | 재시도 성공 (다음 이벤트 수신) | `RUNNING` | rate limit 해소 |
| `RATE_LIMITED` | 재시도 소진 (result error) | `CRASHED` | rate limit 복구 실패 |
| `STOPPING` | 프로세스 종료 | `COMPLETED` | graceful 종료 성공 |
| `STOPPING` | SIGKILL 후 프로세스 종료 | `COMPLETED` | 강제 종료 완료 |

---

## 7. 재시도 및 재개 로직

### 7.1 Rate Limit 처리

Rate limit은 Claude Code 내부에서 자동 재시도된다. SessionManager는 이를 관찰하고 Supervisor에 보고한다. Claude Code 내부 재시도가 소진되어 세션이 종료된 경우에만 Supervisor가 개입한다.

```
[stream-json]
  │
  ▼
api_retry (error=rate_limit, retry_delay_ms=30000, attempt=1/5)
  │
  │  Claude Code 내부: 30초 대기 → 자동 재시도
  │
  ▼
api_retry (error=rate_limit, retry_delay_ms=60000, attempt=2/5)
  │
  │  Claude Code 내부: 60초 대기 → 자동 재시도
  │
  ▼
assistant (text) ← 재시도 성공, 정상 재개
  │
  ▼
(계속 실행...)
```

**Claude Code 내부 재시도 소진 시**:

```
api_retry (attempt=5/5, error=rate_limit)
  │
  ▼
result (subtype=error, is_error=true)
  │
  ▼
[프로세스 종료]
  │
  ▼
SessionManager → Supervisor에 보고
  │
  ▼
Supervisor: rate_limit_base_wait_s 대기 → resume_session() 호출
```

### 7.2 크래시 복구 전략

Supervisor의 ErrorClassifier가 분류한 유형에 따라 SessionManager가 다른 복구 행동을 수행한다.

| 에러 유형 | 복구 전략 | SessionManager 동작 |
|---------------|-----------|---------------------|
| `RATE_LIMIT` | 대기 후 재개 | `resume_session(session_id, "계속 진행하세요.")` |
| `CRASH` (일시적) | 새 세션 | `launch_session(같은 프롬프트)` |
| `CRASH` (반복) | 미션 스킵 | 없음 — Supervisor가 다음 미션 선택 |
| `AUTH` | 대기 (Owner 개입 필요) | 없음 — Supervisor가 장시간 대기 |
| `NETWORK` | 짧은 대기 후 재시도 | `launch_session(같은 프롬프트)` |
| `STUCK` (타임아웃) | 새 세션 | `launch_session(새 프롬프트)` |

```python
# Supervisor에서의 복구 흐름 (SessionManager를 호출하는 측)

async def _recover_from_crash(
    self,
    session_info: SessionInfo,
    category: ErrorType,
) -> None:
    """에러 유형에 따른 복구 실행."""

    match category:
        case ErrorType.RATE_LIMITED:
            # 대기 후 같은 세션 재개
            wait_s = self.config.rate_limit_base_wait_s
            await asyncio.sleep(wait_s)
            await self.session_manager.resume_session(
                session_id=session_info.session_id,
                prompt="Rate limit이 해소되었습니다. 이전 작업을 계속 진행하세요.",
                mission_id=session_info.mission_id,
            )

        case ErrorType.PROCESS_CRASH | ErrorType.NETWORK_ERROR:
            # 짧은 대기 후 새 세션으로 같은 미션 재시도
            await asyncio.sleep(10)
            prompt = await self._prepare_session_for_mission(
                session_info.mission_id
            )
            if prompt:
                await self.session_manager.launch_session(
                    prompt=prompt,
                    mission_id=session_info.mission_id,
                )

        case ErrorType.STUCK:
            # 타임아웃 — 새 프롬프트로 새 세션
            prompt = await self._prepare_session_for_mission(
                session_info.mission_id,
                context_hint="이전 세션이 타임아웃되었습니다. "
                             "다른 접근 방식을 시도하세요.",
            )
            if prompt:
                await self.session_manager.launch_session(
                    prompt=prompt,
                    mission_id=session_info.mission_id,
                )

        case ErrorType.AUTH_FAILURE:
            # Supervisor가 처리 — SessionManager는 아무것도 안 함
            pass
```

### 7.3 타임아웃 처리

`session_timeout_minutes`가 0이 아닌 양수로 설정되어 있으면, 세션 실행 시간이 해당 값을 초과할 때 강제 종료한다.

```python
async def _handle_timeout(self, proc: asyncio.subprocess.Process) -> None:
    """
    세션 타임아웃 처리.
    state를 TIMED_OUT으로 변경하고 프로세스를 강제 종료한다.
    """
    if self._current is not None:
        self._current.state = SessionState.TIMED_OUT
    self.log.warning("세션 타임아웃 — 강제 종료")
    await self._force_terminate(proc)
```

---

## 8. 프로세스 관리

### 8.1 프로세스 시작

```python
# asyncio.create_subprocess_exec 사용
proc = await asyncio.create_subprocess_exec(
    *cmd,
    stdout=asyncio.subprocess.PIPE,   # stream-json 파싱용
    stderr=asyncio.subprocess.PIPE,   # 에러 수집용
    cwd=str(self.project_root),       # 프로젝트 루트에서 실행
    env=self._session_env,            # 격리된 환경 변수
    start_new_session=True,           # 새 프로세스 그룹 생성
)
```

**`start_new_session=True`의 역할**:
- Python의 `os.setsid()`를 호출하여 새 프로세스 세션(그룹)을 생성한다.
- Claude Code가 내부적으로 생성하는 자식 프로세스(서브에이전트 등)가 같은 그룹에 속한다.
- 종료 시 `os.killpg(pgid, signal)`로 그룹 전체를 한 번에 종료할 수 있다.
- Supervisor 자체는 영향받지 않는다.

### 8.2 Graceful 종료

SIGTERM으로 우아한 종료를 시도하고, 시간 내에 종료되지 않으면 SIGKILL로 강제 종료한다. 프로세스 그룹 전체에 시그널을 전송한다.

```python
async def terminate_session(
    self,
    session_id: str,
    *,
    graceful: bool = True,
) -> None:
    """실행 중인 세션을 종료한다."""
    info = self._current
    if info is None or info.session_id != session_id:
        self.log.warning("종료할 세션을 찾을 수 없음: %s", session_id)
        return

    proc = info.process
    if proc is None or proc.returncode is not None:
        self.log.info("프로세스가 이미 종료됨.")
        return

    info.state = SessionState.STOPPING
    pgid = os.getpgid(proc.pid)

    if graceful:
        # Phase 1: SIGTERM을 프로세스 그룹에 전송
        self.log.info(
            "SIGTERM 전송: pid=%d pgid=%d", proc.pid, pgid,
        )
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            self.log.info("프로세스 그룹이 이미 종료됨.")
            return

        # Phase 2: graceful_shutdown_timeout_s 동안 대기
        try:
            await asyncio.wait_for(
                proc.wait(),
                timeout=self.graceful_shutdown_timeout_s,
            )
            self.log.info(
                "graceful 종료 성공: exit_code=%s", proc.returncode,
            )
            info.exit_code = proc.returncode
            info.state = SessionState.COMPLETED
            return
        except asyncio.TimeoutError:
            self.log.warning(
                "SIGTERM 타임아웃 (%s초) — SIGKILL 전송",
                self.graceful_shutdown_timeout_s,
            )

    # Phase 3 (또는 graceful=False): SIGKILL
    await self._force_terminate(proc)


async def _force_terminate(
    self,
    proc: asyncio.subprocess.Process,
) -> None:
    """프로세스 그룹에 SIGKILL을 전송하고 종료를 대기한다."""
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGKILL)
        self.log.info("SIGKILL 전송: pgid=%d", pgid)
    except ProcessLookupError:
        self.log.info("프로세스 그룹이 이미 종료됨.")
        return

    await proc.wait()

    if self._current is not None:
        self._current.exit_code = proc.returncode
        if self._current.state != SessionState.TIMED_OUT:
            self._current.state = SessionState.COMPLETED

    self.log.info("강제 종료 완료: exit_code=%s", proc.returncode)
```

### 8.3 종료 시퀀스 타이밍

```
terminate_session(graceful=True) 호출
  │
  ▼
[T+0s]   os.killpg(pgid, SIGTERM)
  │         프로세스 그룹 전체에 SIGTERM 전송
  │         Claude Code가 현재 작업을 정리할 시간 부여
  │
  │       asyncio.wait_for(proc.wait(), timeout=10)
  │
  ├── [T+Xs] proc.wait() 반환 (X < 10)
  │         → graceful 종료 성공. exit_code 기록.
  │
  └── [T+10s] TimeoutError
                │
                ▼
              os.killpg(pgid, SIGKILL)
                │
                ▼
              [T+10.Xs] proc.wait() 반환
                → 강제 종료 완료. exit_code 기록.
```

---

## 9. 상태 조회

```python
def get_session_status(self) -> SessionStatus:
    """현재 세션 상태 요약을 반환한다."""
    if self._current is None:
        return SessionStatus(is_active=False)

    info = self._current
    running_seconds = 0.0
    if info.ended_at is not None:
        running_seconds = info.ended_at - info.started_at
    elif info.state in (SessionState.RUNNING, SessionState.RATE_LIMITED):
        running_seconds = time.time() - info.started_at

    return SessionStatus(
        is_active=info.state in (
            SessionState.LAUNCHING,
            SessionState.RUNNING,
            SessionState.RATE_LIMITED,
            SessionState.STOPPING,
        ),
        session_id=info.session_id,
        state=info.state,
        mission_id=info.mission_id,
        running_seconds=running_seconds,
        event_count=info.event_count,
        tool_use_count=info.tool_use_count,
        api_retry_count=info.api_retry_count,
        last_event_type=(
            f"{info.last_event.type}/{info.last_event.subtype}"
            if info.last_event else ""
        ),
        last_event_time=(
            info.last_event.timestamp
            if info.last_event else 0.0
        ),
    )
```

---

## 10. 스트림 이벤트 실시간 전달 흐름

SessionManager가 파싱한 이벤트가 Supervisor를 거쳐 여러 소비자에게 전달되는 전체 흐름이다.

```
┌──────────────┐
│ Claude Code  │
│  프로세스     │
│              │
│  stdout      │
│  (stream-    │
│   json)      │
└──────┬───────┘
       │ 줄바꿈 구분 JSON
       ▼
┌──────────────┐
│ Session      │
│ Manager      │
│              │
│ readline()   │
│ json.loads() │
│ → Stream     │
│   Event      │
└──────┬───────┘
       │ AsyncIterator[StreamEvent]
       ▼
┌──────────────┐
│ Supervisor   │
│              │
│ async for    │
│  event in    │
│  monitor()   │
│              │
│ 이벤트 분기: │
└──┬───┬───┬───┘
   │   │   │
   │   │   └──────────▶ ┌──────────────┐
   │   │    TUI 갱신     │ State        │
   │   │                │ Manager      │
   │   │                │              │
   │   │                │ current_     │
   │   │                │ session.json │
   │   │                └──────────────┘
   │   │
   │   └──────────────▶ ┌──────────────┐
   │     Slack 알림      │ Slack Client │
   │     (필요시)        │              │
   │                    │ rate limit,  │
   │                    │ 에러 등 알림  │
   │                    └──────────────┘
   │
   └──────────────────▶ ┌──────────────┐
     로그 기록           │ 로그 파일     │
                        │              │
                        │ session.log  │
                        └──────────────┘
```
