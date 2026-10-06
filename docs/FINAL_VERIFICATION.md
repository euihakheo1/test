# 최종 검토와 실행 결과

검토한 GitHub 원본 커밋은 `874ca5c372ef160c1e0756495c34224b79283871`이다.
코드 검증 커밋은 `f7fceebbce7499c96b5a7520dabde28e0f451bda`이며, 이후 변경은 이 보고서와 문서
정리다. 실행 환경은 Linux, Python 3.12, uv 0.12.19, Node.js 24.19.0, npm 11.9.0이다.
CI·Docker의 uv는 기존 정책대로 0.12.23, Node 이미지는 22를 사용한다. 아래는 2026-10-06 UTC에
직접 실행한 결과이며 이전 개발자의 Windows 검증 보고를 대신 인용한 결과가 아니다.

## 변경 범위

기존 환경 검사, XLSX 사전 제한, HttpOnly 세션·CSRF·갱신·조직 격리, Agent 웹 연결, E2E와 CI를
유지했다. 로컬 vLLM 호출을 기존 LLM gateway에 추가하고 API·worker·화면의 `local` 모드를 연결했다.
로컬 호출은 외부 API 과금 없이 실행되며, 기존 유료 제공자는 `live`·양수 예산 조건을 그대로 따른다.
로컬 호출은 기본적으로 캐시를 재생하지 않는다. 사설 주소 제한, 프록시 환경 변수 무시, 리다이렉트
거부, 오류 본문 비공개, 스키마 검증, 기존 Agent 단계·토큰 제한을 적용했다.

기본 모델은 `Qwen/Qwen3-4B-Instruct-2507`, 가중치 revision은
`cdbee75f17c01a7cc42f958dc650907174af0554`, served model id는
`Qwen3-4B-Instruct-2507-cdbee75`다. 추론 서버는 API 가상환경과 분리한다. 로컬은 loopback,
Compose에서는 내부 네트워크에서만 접근하며 vLLM 포트를 게시하지 않는다.

운영용 웹 Dockerfile, Caddy HTTPS 프록시, vLLM Compose 추가 파일, 비밀값을 출력하지 않는 설정
생성기를 제공했다. 백엔드 이미지는 LLM·Agent extras도 설치한다. Docker 빌드 컨텍스트에서 비밀
환경 파일과 런타임 자료를 제외했다. 기존 라이선스 정책을 유지했고 MIT로 변경하지 않았다.

웹의 도구 실행은 행동 JSON을 검증한 뒤 서버의 typed tool을 호출하는 구조다. 네이티브 OpenAI
`tools` 프로토콜, Hermes, 또는 웹 호출의 MCP 네트워크 경유를 구현한 것으로 설명하지 않는다.
MCP 서버는 별도로 제공된다. 포트폴리오에는 실제 사용한 경로를 표시한다.

## 공개 파일만으로 새 환경에서 실행

검증 커밋을 `git clone --no-local --branch codex/final-vllm . var/fresh-clone`으로 다시 받았다.
초기에 `.env`, `.env.vllm`, `deploy/.env`, `.venv`, `node_modules`, `.next`, DB가 모두 없음을
확인했다. 공개 소스와 lock 파일만으로 다음 명령을 실행했다.

저장소 루트:

```bash
uv sync --frozen --all-extras
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest -q
cp .env.example .env
uv run jettae api migrate
uv run jettae demo run --out-dir var/demo
uv run python scripts/secret_scan.py
```

프런트엔드:

```bash
cd frontend
npm ci
npm run typecheck
npm run lint
npm test
npm run build
```

`npm ci` 후 `.next`가 여전히 없는 상태에서 typecheck를 시작했다. 데모 생성은 공개 의결서 전사
자료로 계정·정산서·입금 CSV·분석 결과를 만들었다. 계정 비밀번호와 생성 파일은 공개 대상에서 제외했다.

| 검사 | 직접 확인한 결과 |
|---|---|
| 백엔드 전체 테스트 | **677 passed, 9 skipped, 1 warning**, 62.96초 |
| Ruff / format | 위반 0, Python 파일 275개 포맷 일치 |
| mypy | source 파일 153개, 오류 0 |
| 프런트엔드 | lint·typecheck·build 통과, 테스트 **45 passed** |
| 새 환경 마이그레이션·데모 | 완료, 정산서·입금 CSV 생성 확인 |
| SQLite 마이그레이션 왕복 | upgrade → downgrade base → upgrade → check, 새 작업 없음 |
| 공개 파일 비밀정보 검사 | code commit의 공개 대상 383개, findings 0 |
| 전체 Git 이력 검사 | code commit까지 5개 커밋·450개 blob, custom scanner findings 0 |
| gitleaks 8.30.1 | 공개 파일 `dir` 검사와 5개 커밋 `git` 검사, 값 가림, leaks 0 |

SQLite 왕복의 정확한 명령:

```bash
uv run alembic heads
uv run alembic -x url=sqlite:///./var/migration-check.db upgrade head
uv run alembic -x url=sqlite:///./var/migration-check.db downgrade base
uv run alembic -x url=sqlite:///./var/migration-check.db upgrade head
uv run alembic -x url=sqlite:///./var/migration-check.db check
```

gitleaks 실행에는 `.gitleaks.toml`을 사용하고 `--redact --no-banner`를 붙였다. 새 clone에 의존성과
런타임 자료를 생성하기 전에 공개 파일 검사를 수행했다. 파일을 `.gitignore`에 넣었는지만 본 것이
아니라 실제 Git 추적·추가 대상과 전체 이력을 검사했다. 발견된 실제 비밀값은 없으며, 어떤 검사도
모든 형태의 비밀값·개인정보를 탐지한다는 보장은 하지 않는다.

## 실제 프로세스 확인

API·worker·웹을 각각 다음 명령으로 실행하고 HTTP로 확인했다.

```bash
uv run jettae api serve --host 127.0.0.1 --port 8000
uv run jettae worker run
```

```bash
cd frontend
npm run start -- --hostname 127.0.0.1 --port 3000
```

`http://127.0.0.1:3000`의 Next 프록시를 거쳐 `/login`, `/check`, `/api/v1/health`,
`/api/v1/ready`가 모두 200이었다. 데모 로그인 200, 응답 본문에 access/refresh token 없음,
인증 쿠키 HttpOnly, `/auth/me` 200, CSRF 헤더 없는 refresh 403, 정상 refresh 200,
logout 204, 이후 `/auth/me` 401을 확인했다. 무료 계산 API 200과 기준일 누락 시
`insufficient` 응답도 확인했다. 첫 HTTP 확인은 잘못된 화면 경로 `/due`로 404가 나왔으며,
실제 화면 경로 `/check`로 수정해 위 결과를 다시 확인했다.

Docker용 Next 출력도 `JETTAE_NEXT_STANDALONE=1 npm run build`로 빌드했다. 생성한
standalone 디렉터리와 static 파일을 Dockerfile의 배치대로 별도 폴더에 복사하고
`HOSTNAME=127.0.0.1 PORT=13081 node var/standalone-web/server.js`로 실행했다.
로그인 화면 200, 참조하는 static asset 9개가 모두 200이었다. 이것은 Next standalone 실행
확인이며 Docker 이미지·컨테이너·Caddy 확인으로 확대하지 않는다.

## 의존성과 CI

`uv export --frozen --all-extras --no-emit-project --format requirements-txt`의 고정 목록으로
pip-audit 2.10.1을 실행했다. 도구 설치의 첫 시도는 다운로드 timeout으로 실패했으며,
누락된 tool wheel을 별도 임시 폴더에서 제공한 재시도는 완료했다.

| 검사 | 결과 |
|---|---|
| Python / PyPI | Linux에 적용되는 103개 패키지, 알려진 취약점 0 |
| Python / OSV | 103개 패키지, 알려진 취약점 0 |
| `npm audit --omit=dev` | 0 vulnerabilities |
| `npm audit` | **5 high**, 개발용 ESLint 경로의 `braces` advisory와 전이 의존성 |

정확한 명령·남은 항목·버전과 예외의 이유는 [dependency-audit.md](security/dependency-audit.md)에
기록했다. 강제 major downgrade를 적용하지 않았다. 별도 vLLM/CUDA/PyTorch 환경, 컨테이너 이미지와
모델 가중치는 이 의존성 검사에 포함되지 않는다.

CI는 백엔드·폐기용 PostgreSQL·프런트엔드·브라우저 E2E와 Docker build로 구성했다. 기본 CI는
offline·예산 0이며 유료 API 키가 있으면 실패한다. 추가한 Docker job은 추론 없이 Compose 설정과
API·웹 이미지 빌드를 검사한다. 새 CI를 GitHub에서 실행한 결과는 아직 없다.

## 실행하지 못한 항목과 남은 제한

- `uv run --with pgserver python deploy/run_pg_tests.py var/pgtest`를 시도했지만 이 환경의 OS
  사용자 생성/전환 제한으로 PostgreSQL을 시작하지 못했다. 9개 skip 중 8개는 PostgreSQL 전용,
  1개는 Windows stdio 전용이다. PostgreSQL 통과로 보고하지 않는다.
- `npm run e2e:install`의 Chromium 다운로드가 zip 대신 HTML을 반환해 설치에 실패했다.
  `npm run e2e`는 API·worker·웹을 시작했지만 브라우저 실행 파일이 없어 **3개 테스트가 실행
  단계에서 실패**했다. 브라우저 E2E 통과로 보고하지 않는다. 앞의 HTTP 검사는 브라우저 검사가 아니다.
- GPU와 Docker가 없어 실제 vLLM 가중치 로딩·LLM 도구 선택 성능·VRAM·처리량·Docker/Compose·
  HTTPS 운영 배포는 확인하지 못했다. 로컬 LLM 회귀 테스트는 SDK의 HTTP 응답을 제어한 통합
  시험이며 실제 모델 정확도가 아니다.
- 실제 홈택스·은행 내보내기 양식, BPI 2019 평가, 전사 정답 검수, 최대 허용 XLSX의 부하·
  장시간 운영은 이번 검토 범위에 포함하지 않았다. 기존 평가 수치를 새 LLM 성능으로 소개하지 않는다.
- GitHub에 push하지 않았다. 전달한 패치와 소스 ZIP에는 공개 코드·설정 예시·문서만 포함한다.
  사용자 비밀값·업로드·DB·캐시·로그와 GPU 모델은 포함하지 않는다.

실행·포트폴리오 캡처·Linux 배포의 순서는 [GETTING_STARTED.md](GETTING_STARTED.md)를 따른다.
vLLM의 외부 API 요금은 0이지만 GPU·전기·서버·도메인 운영비와 상시 가용성은 별도로 확보해야 한다.
테스트 결과는 확인한 범위의 동작을 뜻하며 운영 보안 전체·법률 정확도·LLM 정확도를 보증하지 않는다.
