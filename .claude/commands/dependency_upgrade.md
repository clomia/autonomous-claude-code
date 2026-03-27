---
description: 프로젝트에서 사용되는 모든 의존성을 최신 버전으로 업그레이드합니다.
---

TASK: 프로젝트에서 사용되는 모든 의존성을 최신 버전으로 업그레이드

---

# 확인된 의존성 파일들

## Github workdlow

@.github/workflows/deploy-server.yml

## Frontend dependencies

@app/package.json

- shadcn, bits-ui등에서 next버전을 사용할 수 있으니 주의
  - @app/src/lib/components/ui/
- Typescript, Svelte, Sveltekit, vite 등 메인 패키지도 업그레이드 대상.

## Backend dependencies

### AWS CDK

@server/package.json
@server/lib/aws-stack.ts

### Python API Server

@server/src/api/pyproject.toml
@server/src/api/Dockerfile

### Others

@server/src/jobs/cleanup_email_verifications/pyproject.toml
@server/src/jobs/cleanup_email_verifications/Dockerfile

@server/src/naver_bridge/pyproject.toml
@server/src/naver_bridge/Dockerfile

# 지시

## 작업

- 사용되는 모든 의존성 버전이 최신 버전이 맞는지 조사.
  - **신뢰할 수 있는 공식 registry를 사용**
    - 예: https://pypi.org/ 웹 검색 , https://www.npmjs.com/ 웹 검색, npm view 명령어
  - 지식에 의존하지 말고 반드시 실시간 최신 정보 조사
  - 웹 조사 시 신뢰할 수 있는 공식 정보인지 여부를 반드시 확인
  - 버전의 출시일과 오늘 날짜 비교.
- 더 최신 버전이 있다면 **안전한지 확인 후 교체**
  - 의존성과 연관된 기존 코드의 동작이 보존되는지 확인.
  - 웹 조사로 새로운 버전이 어떤 변경사항을 내포했는지 조사하고 기존 코드와 연관성 확인
- IMPORTANT: **현재 모든 코드는 정상 동작함.** 현재 설치된 모든 패키지는 완전히 정상 동작하며 실제 존재하는 버전임. 조사 결과 현재 버전이 휴효하지 않다고 판단되면 조사 자체에 문제가 있는것.

# 수행

- 먼저, '확인된 의존성 파일들'에 명시되지 않은 다른 의존성 파일이 존재하는지 파일을 탐색하고 있다면 그것도 작업 대상에 포함해.
- 작업이 완료된 후 수행한 업데이트와 더 최신 버전이 있으나 업데이트하지 않은 의존성들을 그 사유와 함께 나열해.
- 이것은 전체 코드베이스를 다루는 대규모 작업이므로 **서브에이전트를 적극적으로 사용해서 작업을 divide and conquer 하도록 해.** ultrathink
