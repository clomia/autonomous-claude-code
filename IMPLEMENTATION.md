# autonomous-claude-code 구현 가이드

---

## 프로젝트 구조

```
autonomous-claude-code/
├── .claude/
│   └── settings.local.json     # 개발용: 제품 hooks 비활성화
├── copier.yml                  # 배포 설정 (dist/ → 사용자 프로젝트)
├── create.py                   # PEP 723 스캐폴드 스크립트
├── design/                     # 설계 문서
├── docs/                       # 독립 문서
├── poc/                        # PoC
├── temp/                       # 임시
└── dist/                       # 제품 코드 (구현 대상)
```

- `design/`, `docs/`, `poc/`, `temp/`: 개발 자료. 배포되지 않음.
- `dist/`: 사용자에게 배포되는 제품 코드. copier가 이 디렉토리만 복사.
- `.claude/settings.local.json`: 개발 시 `dist/.claude/settings.json`의 hooks가 발동하지 않도록 비활성화.

---

## 핵심 문제와 해결

이 프로젝트는 `dist/.claude/settings.json`과 `dist/CLAUDE.md`를 **제품의 일부**로 포함한다. 프로젝트 루트에서 Claude Code로 구현 작업 시, Claude Code가 `dist/` 하위의 설정 파일에 영향받을 수 있다.

**해결**: Claude Code는 `.claude/settings.json`을 **프로젝트 루트**에서만 읽는다. `dist/.claude/settings.json`은 하위 디렉토리이므로 구현 에이전트에게 영향을 주지 않는다. CLAUDE.md는 하위 디렉토리 접근 시 on-demand 로드될 수 있으므로, 구현 시 `dist/CLAUDE.md`는 존재하지 않는 상태가 정상이다 (Initialization Session이 생성).

---

## 배포 전략

사용자 경험:
```bash
uv run https://raw.githubusercontent.com/<owner>/autonomous-claude-code/main/create.py my-agent
cd my-agent
uv sync
uv run acc configure
uv run acc start
```

`create.py`는 PEP 723 inline metadata로 copier를 의존성 선언한다. `uv run`이 copier를 임시 환경에 설치하고 `copier.yml`의 `_subdirectory: dist` 설정에 따라 `dist/` 내용만 사용자 프로젝트로 복사한다.

---

## 구현 순서

1. `dist/` 하위에 제품 코드 구현 (설계 문서 기반)
2. `dist/.claude/settings.json` 작성 (제품 hooks)
3. `dist/pyproject.toml.jinja` 작성 (Jinja 템플릿)
4. `copier.yml` 완성 (질문, 제외 패턴, 후처리)
5. 통합 테스트: `uv run create.py test-project`로 스캐폴드 검증
