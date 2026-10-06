# 제때받기 (jettae)

소규모 납품업체 담당자가 정산서·공제 내역·세금계산서·입금 내역·약정서를 올리면, 문서와 입금을
연결해 **금액 차이와 지급 지연**을 보여 주고, 계산 근거가 부족하면 **필요한 서류**를 안내하는
도구입니다. 모든 결과에는 원문 위치(문서 버전·시트/행/열 또는 쪽/문자 범위)와 계산 가정이 붙습니다.
검산 성능은 **실제 공개 자료**(공정위 의결서, BPI Challenge 2019, 공정위 표준거래계약서, 법령·고시
원문)로만 평가합니다. 합성 데이터셋은 쓰지 않습니다.

결과는 "차이 / 필요 서류 / 확인이 필요한 조건 / 관련 근거"로만 표현하며 법적 판단이 아닙니다.
상세 명세는 `docs/SPEC.md`, 모듈 계약은 `docs/ARCHITECTURE.md`, 운영은 `docs/runbook.md`,
평가 절차는 `docs/eval_protocol.md`, 진행 기록과 미검증 항목은 `docs/PROGRESS.md`에 있습니다.
공개 이용 조건은 `docs/PUBLIC_USE.md`, 보안 제보는 `SECURITY.md`를 보세요.

## 목차

처음 실행하는 Windows 사용자는 [PowerShell·WSL 실행과 포트폴리오 준비](docs/GETTING_STARTED.md)를
순서대로 따른다. 무료 로컬 vLLM, API·worker·웹 터미널, 실제 호출 확인과 전체 운영 Compose 명령이 있다.
이번 마무리 검증은 [FINAL_VERIFICATION.md](docs/FINAL_VERIFICATION.md)에 별도로 기록한다.

1. [하는 것 / 하지 않는 것](#하는-것--하지-않는-것)
2. [저장소에 있는 것과 없는 것](#저장소에-있는-것과-없는-것)
3. [준비물과 설치](#준비물과-설치)
4. [설정 파일(.env) 읽는 규칙](#설정-파일env-읽는-규칙)
5. [로컬 데모: API · worker · 화면](#로컬-데모-api--worker--화면)
6. [자기 자료와 공개 자료로 실행](#자기-자료와-공개-자료로-실행)
7. [Agent 조사와 MCP](#agent-조사와-mcp)
8. [실제 LLM 실행(유료 호출)](#실제-llm-실행유료-호출)
9. [운영 배포](#운영-배포)
10. [테스트 · E2E · CI · 보안 검사](#테스트--e2e--ci--보안-검사)
11. [구현 · 검증 · 미검증](#구현--검증--미검증)

## 하는 것 / 하지 않는 것

하는 것

- 거래별 대사: 미입금·부분 입금·차액·합산/분할 입금·공제·환불·수수료·같은 금액 모호 거래
  (`MATCHED | PARTIAL | UNMATCHED | AMBIGUOUS | CONFLICT | INSUFFICIENT_EVIDENCE`)
- 대규모유통업법 제8조(직매입 상품수령일+60일, 특약매입 등 판매마감일+40일)·하도급법 제13조와
  지연이율 고시(연 15.5%)로 지급기한·지연일수·지연이자를 **코드로** 계산. 휴일 이월(rollover)·
  원 단위 처리는 설정값이며, 정할 수 없으면 두 계산과 미확인 조건을 함께 보여 줌
- 기준일(상품수령일·판매마감일)이 없으면 계산하지 않고 필요한 서류 안내(세금계산서 작성일로
  대체하지 않음). 입금 대상이 특정되지 않은 거래(AMBIGUOUS)는 지급기한만 보여 주고
  지연일수·지연이자는 계산하지 않음
- 새 입금·정정 문서·추가 약정이 들어오면 영향받는 결과만 다시 계산(전체 재계산과 같은 결과임을
  테스트·E6로 확인)하고, 승인된 결과가 바뀌면 `REVIEW_REQUIRED`로 표시(과거 승인 기록은 유지)
- 결과 상세 화면에서 Agent 조사(단일 Agent 또는 역할 분담): 부족한 근거와 필요한 서류, 인용 원문을
  정리. 조사 결과는 엔진이 계산한 수치를 바꾸지 않음
- 회사(테넌트)별 격리, 업로드 검사(XLSX 행·열·셀·병합 영역 상한 포함), CSV formula injection 방지,
  HttpOnly 쿠키 세션과 CSRF 검사, 감사 가능한 결과 해시

하지 않는 것(첫 버전): 위법 여부 판정, 법적 권리 확정, 회수 가능성 예측, 보험금 산정,
회생·세금·대출 판단, 외부 청구·독촉·대리, 성공보수, PG 정산자금 보호 안내. 2026.9.17 국회 통과
개정안(35일/20일)은 시행일이 정해지기 전까지 비활성 규칙 버전으로만 등록되어 있습니다.

### 기준일(as_of)과 약정 기한의 뜻

- `as_of`는 **미지급분 지연일수를 세는 기준일**입니다. `as_of` 이후 입금일의 입금은 그 분석에서
  제외되고 미지급분은 `as_of`까지 지연일수를 셉니다. 과거 어느 시점의 문서·정정 상태를 되살리는
  **전체 bitemporal 복원은 아닙니다**.
- `known_at`은 **규칙 버전 선택에만** 쓰입니다(그 날짜에 알려진 법령·고시 버전으로 계산).
- 약정서의 지급 일수로 낸 **약정 기한**은 법정 지급기한과 별도로 표시하는 날짜이며, 법정
  지연이율을 적용하지 않습니다. 약정의 기산점은 추출하지 않으므로 법정 기준일을 가정하고 그 가정을
  확인 필요 조건으로 표시합니다.

### 문서 행과 미수 채권

정산서의 한 행과 같은 거래의 세금계산서는 **하나의 채권**입니다. 금액은 '금액 기준' 문서 하나(정산
행이 있으면 정산 행)에서만 가져오고, 세금계산서는 같은 거래처 + 같은 참조번호(정산 행 하나와만
일치)이거나 사용자가 확인했을 때만 보강 증빙으로 연결합니다. 금액·날짜가 같다는 이유만으로는 연결하지
않습니다. 연결이 불확실한 세금계산서는 '확인 대기'(AMBIGUOUS)로 표시되고 미수 합계와 입금 배분에서
빠지며, 결과 상세 화면에서 "이 정산 행과 같은 거래 / 별개 거래"를 고르면 영향받는 결과만 다시
계산합니다. 연결된 문서의 금액이 다르면 CONFLICT로 표시합니다.

## 저장소에 있는 것과 없는 것

같은 저장소로 로컬 데모, 실제 LLM 실행, 운영 배포를 모두 합니다. 차이는 **코드가 아니라 설정 파일**
입니다. 보안·운영 코드(인증, CSRF, 시작 단계 설정 검사, 업로드 검사, 배포 파일)는 저장소에 그대로
있고, 실제 값과 자료만 저장소 밖에 둡니다.

| 저장소에 있음(공개 대상) | 저장소에 없음(`.gitignore`, 공개 대상 아님) |
|---|---|
| 소스·테스트·마이그레이션·배포 파일(`deploy/`)·CI(`.github/`) | 실제 `.env`, `.env.live-llm`, `.env.prod`, `frontend/.env.local` 등 값이 든 설정 파일 |
| 가짜 값만 든 설정 예시 6개(아래 표) | API 키, JWT 서명 키, DB 비밀번호, 본인 법제처 OC |
| 공개 자료의 수집 기록(`data/manifests/`), 공정위 의결서 표 전사와 본문 수치·짧은 본문 발췌(`data/seeds/`, `docs/PUBLIC_USE.md` 2절), 평가 요약(`docs/eval_results.md`) | 내려받은 원본(`data/raw/`), 다시 만들 수 있는 평가 결과 JSON(`data/results/`) |
| 손으로 쓴 테스트 입력(`tests/`, `frontend/e2e/fixtures/`) | 사용자가 올린 파일·DB·LLM 캐시·비용 원장(`var/`), `*.db`, 로그(`*.log`, `logs/`), 캐시(`.mypy_cache` 등, `node_modules`, `.next`, E2E 결과), 개인 도구 설정(`.npmrc`, `.claude/settings.local.json` 등) |

`python scripts/secret_scan.py`가 공개 대상 파일 전체와 모든 커밋 이력을 검사하고, CI는 gitleaks
(`.gitleaks.toml`, 값은 가림)로 모든 커밋을 한 번 더 검사합니다. 결과에는 규칙 이름·경로·줄 번호만
나오고 찾은 값은 출력하지 않습니다. 구분 기준 전체는 `docs/PUBLIC_USE.md` 3절.

## 준비물과 설치

필요한 것:

- Git
- [uv](https://docs.astral.sh/uv/) (Python 3.12는 uv가 준비합니다. 없으면 `uv python install 3.12`)
- Node.js 22.18 이상과 npm(화면, `frontend/package.json`의 `engines`)
- 인터넷(의존성 설치, 공개 자료 수집)

저장소를 받은 뒤 그 폴더(이하 "저장소 루트")에서 실행합니다. 아래 명령은 Linux·macOS 셸과 Windows의
Git Bash에서 그대로 동작합니다.

```bash
uv sync --frozen --all-extras     # uv.lock 그대로 설치(백엔드 + 개발 도구 + llm/agents/postgres extra)
uv run jettae --help              # 하위 명령: modules rules ingest eval agent ocr demo sources mcp api worker
```

```bash
cd frontend
npm ci                            # package-lock.json 그대로 설치
cd ..
```

Windows에서 긴 경로(LongPathsEnabled)가 꺼져 있고 저장소 경로가 길면 가상환경을 짧은 경로에 둡니다:
`export UV_PROJECT_ENVIRONMENT="$HOME/.venvs/jettae"`를 먼저 실행하고 같은 셸에서 `uv` 명령을 씁니다.

## 설정 파일(.env) 읽는 규칙

설정 예시 파일은 여섯 개이며 모든 비밀값 자리는 `change-me-...` 같은 가짜 값입니다.

| 예시 파일 | 쓰임 | 복사해서 쓰는 이름(커밋 금지) |
|---|---|---|
| `.env.example` | 로컬 데모·개발(`JETTAE_ENV=dev`, SQLite, LLM 오프라인·예산 0) | `.env` |
| `.env.vllm.example` | 무료 로컬 추론(`local`, 사설 vLLM, 유료 예산 0) | `.env.vllm` |
| `.env.live-llm.example` | 실제 LLM 실행(공급자·모델·API 키·예산·환율·가격·공유 예산·회사 동의) | `.env.live-llm` |
| `.env.prod.example` | 운영(PostgreSQL, JWT 서명 키, 허용 출처, Secure 쿠키, 본인 DRF OC, worker 설정) | 서버의 비밀 저장소 |
| `frontend/.env.example` | 화면(Next.js) 빌드 설정 | `frontend/.env.local` |
| `deploy/.env.example` | Docker Compose 운영 배포(PostgreSQL 비밀번호, JWT 서명 키, 허용 출처, 네트워크) | `deploy/.env` |

백엔드(모든 `jettae` 명령: api, worker, mcp, demo, agent, ocr, eval, sources ...):

1. `JETTAE_ENV_FILE`이 있으면 **그 파일만** 읽습니다(상대 경로는 명령을 실행한 폴더 기준, 없는 파일이면
   오류).
2. 없으면 **명령을 실행한 폴더의 `./.env`**를 읽습니다(없으면 건너뜀). 그래서 API·worker·데모는 모두
   저장소 루트에서 실행합니다. 상대 경로 설정(`sqlite:///./var/jettae.db`, `./var/blobs`)도 이 폴더
   기준입니다.
3. 셸에 이미 있는 환경 변수가 파일 값보다 **우선**합니다.
4. `JETTAE_ENV`는 `dev`, `test`, `prod`만 허용합니다. `production`, `staging` 같은 값은 시작 단계에서
   거부됩니다. `prod`에서는 각 명령이 **자기가 쓰는 설정만** 시작할 때 검사하고, 빠지거나 예시 값이면
   **값은 출력하지 않고** 항목 이름만 알린 뒤 종료합니다.

   | 검사 항목(`prod`) | api | worker | mcp | db(`api migrate`, `demo`) | sources(ftc·law) |
   |---|---|---|---|---|---|
   | `JETTAE_DATABASE_URL`이 PostgreSQL이고 비밀번호가 예시 값이 아님 | 예 | 예 | 예 | 예 | |
   | `JETTAE_JWT_SECRET` 32바이트 이상, 예시 값 아님 | 예 | | 예 | | |
   | `JETTAE_ALLOWED_ORIGINS`가 https, `JETTAE_COOKIE_SECURE`가 false 아님 | 예 | | | | |
   | `JETTAE_LLM_MODE=live`이면 `JETTAE_LLM_BUDGET_KRW` 0 초과·유한(100억 원 이하), `JETTAE_LLM_BUDGET_DB` PostgreSQL | 예 | 예 | 예 | | |
   | 본인 `JETTAE_DRF_OC`(샘플 `test`·예시 값 아님) | | | | | 예 |

   그래서 예를 들어 `jettae worker run`은 JWT 서명 키가 예시 값이어도 시작합니다(worker는 토큰을
   만들지 않음). 세부는 `docs/runbook.md` 2절.

화면(Next.js, `frontend/`):

- Next.js는 `frontend/.env.local`을 읽습니다(저장소 루트의 `.env`는 읽지 않음).
- `JETTAE_API_ORIGIN`(API 주소, rewrites 대상)과 `NEXT_PUBLIC_*` 값은 **`npm run build` 때 빌드 결과에
  고정**됩니다. 바꾸면 다시 빌드합니다. `npm run dev`는 시작할 때 읽습니다.
- 브라우저는 같은 출처의 `/api/v1/*`만 부르고 Next가 API로 넘깁니다. 로그인은 HttpOnly 쿠키이며 화면은
  토큰을 저장하지 않습니다.

## 로컬 데모: API · worker · 화면

데모 자료는 `data/seeds/ftc_rows.csv`(**공정위 의결서 표에서 옮긴 실제 사례, 전사 검증 전**)의 값을
그대로 옮겨 만든 정산서·입금 CSV입니다. 새로 지어낸 값은 없으며, 기준일이 인쇄되지 않은 표의 행은
계산 보류(필요 서류 안내)로 나옵니다. `JETTAE_ENV=dev`에서만 실행됩니다.

### 0단계: 설정과 데모 자료(한 번)

저장소 루트에서:

```bash
cp .env.example .env
uv run jettae api migrate                       # ./var/jettae.db 생성·최신 스키마
uv run jettae demo run --out-dir var/demo       # 데모 회사·사용자 생성, 업로드, worker 1회, 결과 요약
```

마지막 줄에 데모 로그인 이메일과 비밀번호가 **한 번만** 출력됩니다(어디에도 저장하지 않음). 화면
로그인에 쓰므로 적어 둡니다. 다시 실행하면 새 데모 회사가 하나 더 생깁니다.

`.env`의 `JETTAE_JWT_SECRET`이 비어 있으면 API가 실행할 때마다 임시 키를 만들어, API를 다시 시작하면
로그인이 풀립니다. 고정하려면 아래 값을 `.env`의 `JETTAE_JWT_SECRET=` 뒤에 넣습니다(공유·커밋 금지).

```bash
uv run python -c "import secrets; print(secrets.token_urlsafe(48))"
```

### 터미널 1: API

```bash
uv run jettae api serve --port 8000
```

`http://127.0.0.1:8000/api/v1/docs`(OpenAPI), `http://127.0.0.1:8000/api/v1/ready`(DB·마이그레이션 상태).

### 터미널 2: worker

```bash
uv run jettae worker run
```

문서 읽기, 분석, 변경 적용, Agent 조사 작업을 DB 작업 큐에서 가져가 실행합니다. API와 **같은 폴더,
같은 설정**으로 실행해야 같은 DB를 씁니다.

### 터미널 3: 화면

```bash
cd frontend
cp .env.example .env.local      # JETTAE_API_ORIGIN=http://127.0.0.1:8000
npm run build
npm run start                   # http://localhost:3000
```

개발 중에는 `npm run build`와 `npm run start` 대신 `npm run dev`를 씁니다.

화면 흐름: 로그인(데모 계정) → 결과(상태·차액·지급기한·지연일수·지연이자·필요 서류) → 상세(원문 위치,
규칙 출처, 검사, 이력, Agent 조사, 확인) → 업로드(끌어다 놓기) → 열 매핑 확인(모르는 양식) → 분석 →
정정 업로드(`/changes`) → 재검토 → 보고서(CSV/HTML). 로그인 없는 `/check`(무료 검산)는
`POST /api/v1/public/due`를 부릅니다.

## 자기 자료와 공개 자료로 실행

### 자기 자료

화면에서 가입한 뒤 정산서·입금 내역·세금계산서 파일(CSV, XLSX, 텍스트 PDF)을 올립니다. 업로드한 파일은
`JETTAE_BLOB_DIR`(기본 `./var/blobs`)과 DB에만 저장되며 저장소에 넣지 않습니다. 명령줄로 양식만 확인할
수도 있습니다(파일 이름은 자기 파일로 바꿉니다):

```bash
uv run jettae rules list                                   # 규칙 버전(비활성 포함)·출처
uv run jettae rules due --type direct --base 2025-08-07 --paid 2025-10-20 --amount 10000000
uv run jettae ingest inspect var/demo/demo_settlement.csv  # 양식 인식·열 매핑 제안
uv run jettae ingest parse var/demo/demo_bank.csv --out var/rows.json   # 행·원문 위치·레코드·사실
```

### 실제 공개 자료: 수집 → 변환 → 평가

모든 원천은 `data/manifests/*.json`에 URL·수집 시각·해시·라이선스·실패 기록이 남고, 받은 원본은
`data/raw/`(공개 대상 아님)에 저장됩니다. 출처 표시와 이용 조건은 `docs/PUBLIC_USE.md`.

| 원천 | 명령 | 비고 |
|---|---|---|
| 공정위 의결서(법제처 DRF `target=ftc`) | `uv run jettae sources ftc fetch` → `uv run jettae sources ftc images` → `uv run jettae sources ftc facts` | 개발은 샘플 키 `JETTAE_DRF_OC=test`, 운영·대량 수집은 본인 OC. `--offline`은 캐시만 사용 |
| 법령·고시 원문 | `uv run jettae sources law fetch` → `uv run jettae sources law check` | 규칙의 기한·이율·시행일을 원문과 대조 |
| 공정위 표준거래계약서 | `uv run jettae sources law contract-fetch` → `uv run jettae sources law contract-extract` | HWP 5.0 내장 판독기 |
| BPI Challenge 2019(4TU) | `uv run jettae sources bpi2019 fetch` → `uv run jettae sources bpi2019 convert` → `uv run jettae sources bpi2019 stats` | 4TU 점검 중이면 종료 코드 3과 재시도 안내 |

평가(SPEC §6, 결과는 `data/results/*.json`(공개 대상 아님)과 `docs/eval_results.md`). 저장소의 전사 자료만으로 실행되는
평가와, 공개 자료를 먼저 받아야 하는 평가가 있습니다.

```bash
uv run jettae eval ftc --out var/ftc_eval.json --md var/ftc_eval.md     # E1·E2·E3 (추적 파일을 덮어쓰지 않는 실행)
uv run jettae eval recompute                                           # E6 전체 vs 선택적 재계산
uv run jettae eval contract                                            # E5 (contract-fetch 후; 원본이 없으면 종료 코드 3, 요약 유지)
uv run jettae eval bpi                                                 # E4 (bpi2019 fetch·convert 후)
```

`data/results/*.json`은 저장소에 넣지 않습니다(`.gitignore`). 위 명령으로 언제든 다시 만들 수 있고,
검토한 요약 숫자는 추적 파일 `docs/eval_results.md`에 있습니다. `--out`·`--md` 없이
`uv run jettae eval ftc`를 실행하면 `docs/eval_results.md`의 해당 블록도 갱신합니다.
`eval recompute`·`eval contract`·`eval bpi`는 항상 `docs/eval_results.md`를 갱신하며, 실행 시간 같은
값은 실행마다 달라집니다. 결과를 바꿀 의도가 없으면 실행 후 `git diff`로 확인하고 되돌립니다. 절차와
지표 정의는 `docs/eval_protocol.md`.

## Agent 조사와 MCP

### 화면에서

결과 상세 화면의 "Agent 조사"에서 방식(`single` 단일 Agent 또는 `roles` 역할 분담)과 실행 모드를
고릅니다.

- `offline`(기본): 규칙 기반 계획, LLM 호출 없음.
- `local`: 서버가 설정한 사설 vLLM을 실제 호출(외부 API 과금 0, [실행 절차](docs/GETTING_STARTED.md)).
- `replay`: 기록된 LLM 응답만 재생(없으면 `failed`, `replay_miss`).
- `live`: 서버 설정이 허용할 때만 보입니다([실제 LLM 실행](#실제-llm-실행유료-호출)). 허용하지 않는
  서버에 요청하면 조사는 `refused`로 끝나고 유료 호출은 없습니다.

조사 결과(부족한 근거, 필요 서류, 인용 원문 위치·발췌, 사용량·비용)는 참고 정보이며 엔진의 금액·
지급기한·지연일수·지연이자를 바꾸지 않습니다. API: `POST /api/v1/decisions/{id}/investigations`
(본문 `{"strategy": "single", "mode": "offline"}`), `GET /api/v1/decisions/{id}/investigations`,
`GET /api/v1/investigations/capabilities`.

### 명령줄에서(API 토큰)

CLI·MCP는 쿠키가 아니라 API 토큰(`jtk_...`, 관리자 이상)을 씁니다. 아래는 데모 계정으로 토큰을 만들고
첫 결과를 Agent로 검토하는 순서입니다. API(터미널 1)가 떠 있어야 하며, `DEMO_EMAIL`과 `DEMO_PASSWORD`에는
`jettae demo run`이 출력한 값을 넣습니다.

```bash
export DEMO_EMAIL='데모 이메일'
export DEMO_PASSWORD='데모 비밀번호'
B=http://127.0.0.1:8000/api/v1
curl -s -c var/cookies.txt -H 'content-type: application/json' -X POST "$B/auth/login" \
  -d "{\"email\":\"$DEMO_EMAIL\",\"password\":\"$DEMO_PASSWORD\"}" > /dev/null
CSRF=$(awk '$6=="jt_csrf"{print $7}' var/cookies.txt)
export JETTAE_API_TOKEN=$(curl -s -b var/cookies.txt -H "X-CSRF-Token: $CSRF" \
  -H 'content-type: application/json' -X POST "$B/auth/api-tokens" -d '{"name":"cli","role":"member"}' \
  | uv run python -c "import json,sys; print(json.load(sys.stdin)['token'])")
DECISION_ID=$(curl -s -H "Authorization: Bearer $JETTAE_API_TOKEN" "$B/decisions?limit=1" \
  | uv run python -c "import json,sys; print(json.load(sys.stdin)['items'][0]['id'])")
uv run jettae agent run --strategy single --planner heuristic --decision "$DECISION_ID"
uv run jettae agent run --strategy roles --planner heuristic --decision "$DECISION_ID"
```

`--planner heuristic`은 LLM을 부르지 않습니다. 기본값 `--planner llm --mode replay`는 기록된 응답만 쓰고,
로컬 실제 호출은 `.env.vllm` 설정 후 `--mode local`, 유료 호출은 `--mode live`와 아래 유료 LLM
설정을 사용합니다. [실행 순서](docs/GETTING_STARTED.md)를 따릅니다. `var/cookies.txt`에는 로그인
쿠키가 들어 있으니 끝나면 지웁니다(`rm var/cookies.txt`).

### MCP 서버

```bash
uv run jettae mcp serve                                         # stdio, JETTAE_API_TOKEN 사용
uv run jettae mcp serve --transport streamable-http --port 8765 # 요청별 Authorization: Bearer <토큰>
```

MCP와 API는 같은 application service를 부릅니다. 테넌트는 토큰으로 서버가 정하며 클라이언트가 보낸
`tenant_id`는 무시합니다. 승인·외부 발송 도구는 없습니다.

## 실제 LLM 실행(유료 호출)

기본값은 **오프라인, 유료 호출 예산 0원**입니다. API 키가 있다는 이유만으로 호출하지 않습니다.
실제 호출은 아래가 **모두** 맞을 때만 일어납니다.

| 설정 | 필수 | 뜻 |
|---|---|---|
| `JETTAE_LLM_MODE=live` | 예 | `offline`/`replay`는 기록된 응답만 사용 |
| `JETTAE_LLM_BUDGET_KRW` | 예(0보다 크고 100억 이하인 유한한 수) | 누적 상한(원). 같은 예산 저장소에 기록된 지출 전체의 상한이며 실행마다 초기화되지 않음. `Infinity`·`1e400`·`NaN`은 거부 |
| `JETTAE_LLM_PROVIDER` | 예 | `anthropic`(기본) 또는 `openai` |
| `JETTAE_LLM_MODEL` / `JETTAE_OPENAI_MODEL` | openai는 필수 | 모델 id. anthropic은 비우면 코드 기본값 |
| `ANTHROPIC_API_KEY` 또는 `OPENAI_API_KEY` | 예 | 공급자 SDK가 읽는 키. 설정 파일에만 두고 커밋하지 않음 |
| `JETTAE_LLM_KRW_PER_USD` | 내장 가격표 모델(Anthropic)에 필수 | 원/달러 환율. 코드는 환율을 가정하지 않음 |
| `JETTAE_LLM_PRICES` | 내장 가격이 없는 모델(OpenAI 전부)에 필수 | `{"모델 id": {"input_krw_per_mtok": "원", "output_krw_per_mtok": "원"}}` (백만 토큰당 원) |
| `JETTAE_LLM_ALLOW_UNKNOWN_PRICE` | 아니오 | `1`이면 가격 모르는 모델 허용(비용 null 기록). 기본 거부 |
| `JETTAE_LLM_BUDGET_DB` | 서비스(API+worker)에서 권장, `prod`에서 PostgreSQL 필수 | 공유 예산 저장소(SQLAlchemy URL, 마이그레이션 0004의 `llm_budget*` 표). 보통 `JETTAE_DATABASE_URL`과 같은 값 |
| `JETTAE_LLM_LEDGER` | `JETTAE_LLM_BUDGET_DB`가 없을 때 | 파일 원장의 **절대 경로**. 비우면 사용자별 기본 경로(`JETTAE_STATE_DIR` 또는 OS별 상태 폴더) |
| `JETTAE_LLM_BUDGET_ID` / `JETTAE_LLM_RESERVATION_TTL_S` | 아니오 | 예산 행 이름(기본 `default`) / 정산 안 된 예약을 중단된 것으로 볼 시간(기본 900초) |
| `JETTAE_TENANT_SETTINGS` | 회사 자료를 보내는 호출에 필수 | JSON **파일 경로**. 내용 `{"<회사 id>": {"allow_external_llm": true}}`. 파일이 없으면 모든 회사 거부 |
| `JETTAE_LLM_CACHE_DIR`, `JETTAE_LLM_TIMEOUT_S`, `JETTAE_LLM_MAX_RETRIES` | 아니오 | 재생 캐시 폴더(회사 자료 포함, 공개 대상 아님), 시간 제한, 재시도 |
| `JETTAE_OCR_PROVIDER=vlm` | 아니오 | 스캔 PDF를 VLM으로 읽음(같은 예산·동의 규칙). 없으면 스캔 PDF는 `UNSUPPORTED_SCAN` |

호출 전 최대 비용을 예약하고, 같은 예산 저장소를 쓰는 모든 프로세스의 정산액 + 진행 중 예약이 상한을
넘으면 호출하지 않습니다. 시간 초과·5xx처럼 과금됐을 수 있는 실패는 예약액 전액을 쓴 것으로 기록합니다.

순서(저장소 루트, 터미널마다 같은 `JETTAE_ENV_FILE`):

```bash
cp .env.live-llm.example .env.live-llm
# .env.live-llm 을 편집: API 키, JETTAE_LLM_KRW_PER_USD(또는 JETTAE_LLM_PRICES), 예산을 실제 값으로
export JETTAE_ENV_FILE=.env.live-llm
uv run jettae api migrate
```

회사 동의 파일: 화면에 로그인한 뒤 `GET /api/v1/auth/me`의 `tenant.id`(화면 상단 회사 이름의 회사)를
확인하고, `.env.live-llm`의 `JETTAE_TENANT_SETTINGS` 경로(기본 `./var/tenant_settings.json`)에 아래 형식으로
저장합니다. `TENANT_ID`에는 확인한 값을 넣습니다.

```bash
export TENANT_ID='확인한 회사 id'
uv run python -c "import json,os; open('var/tenant_settings.json','w').write(json.dumps({os.environ['TENANT_ID']: {'allow_external_llm': True}}))"
```

그다음 터미널 1·2를 `export JETTAE_ENV_FILE=.env.live-llm` 후 `uv run jettae api serve --port 8000`과
`uv run jettae worker run`으로 다시 시작하면 화면의 Agent 조사에 `live`가 나타납니다. 명령줄 예:

```bash
uv run jettae agent run --strategy single --mode live --planner llm --decision "$DECISION_ID"
uv run jettae ocr ftc-tables --decision 19065 --limit 3        # 공개 표 이미지 VLM 전사(회사 자료 아님)
```

**검증 범위**: 유료 호출은 이 저장소의 개발 과정에서 한 번도 실행하지 않았습니다. 테스트는 가짜·재생
공급자만 쓰며, 실제 모델의 판독·조사 품질은 검증되지 않았습니다.

## 운영 배포

웹·HTTPS 프록시·내부 vLLM까지 포함한 전체 배포는 [실행 안내 8절](docs/GETTING_STARTED.md#8-linux-서버에-계속-운영하기)을
따릅니다. 아래는 기존 API·worker 배포와 설정 검사 설명입니다. 기본 Docker 이미지에도 `llm`, `agents`
extra가 포함되어 모델 호출과 LangGraph가 배포에서 빠지지 않습니다.

운영은 `JETTAE_ENV=prod`, PostgreSQL, HTTPS 역프록시 뒤의 API·worker·화면입니다.

1. **비밀값은 환경으로**: `.env.prod.example`을 서버의 비밀 저장소(systemd `EnvironmentFile`, 컨테이너
   secret, 플랫폼 환경 변수)로 옮겨 실제 값으로 채웁니다. 파일로 둘 때는 저장소 밖 경로에 두고 권한을
   줄인 뒤 `JETTAE_ENV_FILE`로 지정합니다. 실제 값은 저장소에 커밋하지 않습니다.
2. **시작 단계 검사**: `prod`에서 각 명령은 자기가 쓰는 설정이 빠지거나 예시 값이면 시작하지 않습니다
   ([설정 파일 4번의 표](#설정-파일env-읽는-규칙)). API는 JWT 서명 키·PostgreSQL·https 허용 출처·Secure
   쿠키를, worker와 마이그레이션은 PostgreSQL URL(과 worker의 live LLM 예산)을 검사합니다.
3. **마이그레이션**: 배포마다 API·worker보다 먼저 `jettae api migrate`(또는 `alembic upgrade head`)를
   실행하고, 그 전에 DB와 파일 저장소를 백업합니다(`docs/runbook.md` 6·8절).
4. **프로세스**: API `jettae api serve --host 127.0.0.1 --port 8000 --workers 2`, worker
   `jettae worker run`(여러 개 가능), 화면은 `JETTAE_API_ORIGIN`을 내부 API 주소로 두고 `npm run build`
   후 `npm run start`.
5. **HTTPS 역프록시**: 한 도메인 아래 `/api/`는 **역프록시가 API로 직접** 보내고, 나머지만 Next 화면으로
   보냅니다. 운영에서 `/api/`를 Next의 rewrite로 넘기지 않습니다: Next는 `X-Forwarded-For`를 붙이지 않아
   API가 모든 사용자를 같은 IP(127.0.0.1)로 봅니다. TLS는 역프록시에서 끝냅니다. `jettae api serve`는
   프록시 헤더(`X-Forwarded-For`)를 읽되 uvicorn의 `FORWARDED_ALLOW_IPS` 환경 변수(기본 `127.0.0.1`)에 있는
   프록시만 믿습니다. 프록시가 다른 호스트나 컨테이너 밖에 있으면 그 IP를 넣어야 가입·로그인 속도 제한이
   실제 클라이언트 IP로 셉니다(Docker Compose 파일은 네트워크 게이트웨이를 넣어 둠). 토큰 갱신
   (`/auth/refresh`)은 IP가 아니라 세션별로 제한하므로, 한 IP의 실패한 로그인이 다른 사용자의 갱신을 막지
   않습니다. 공유 속도 제한은 역프록시에 둡니다(`docs/runbook.md` 11절).
6. **Docker Compose**: `deploy/Dockerfile`(API·worker 공용 이미지)과 `deploy/docker-compose.yml`(PostgreSQL 16,
   1회성 migrate, api, worker)이 있습니다. 화면과 역프록시는 포함하지 않습니다. 이 파일들은 Docker가 없는
   개발 PC에서 **빌드·실행해 보지 않았습니다**.

```bash
cd deploy
cp .env.example .env            # POSTGRES_PASSWORD, JETTAE_JWT_SECRET, JETTAE_ALLOWED_ORIGINS 를 실제 값으로
docker compose up -d --build
```

API 컨테이너는 Compose 네트워크의 게이트웨이(기본 `172.31.250.1`, 서브넷 `172.31.250.0/24`)에서 온
`X-Forwarded-For`만 믿습니다. 서버의 다른 네트워크와 겹치면 `deploy/.env`에서 `JETTAE_COMPOSE_SUBNET`과
`JETTAE_PROXY_GATEWAY`를 함께 바꿉니다.

백업·복구·롤백·환경 변수 전체 목록은 `docs/runbook.md`에 있습니다.

## 테스트 · E2E · CI · 보안 검사

테스트는 셸의 `JETTAE_*`·`ANTHROPIC_*`·`OPENAI_*` 변수와 `JETTAE_ENV_FILE`, 작업 폴더의 `.env`를 읽지
않습니다(`tests/_plugins/jettae_testenv.py`가 시작할 때 지움; `JETTAE_TEST_PG_URL`만 남김). 그래서 실제
LLM 설정을 `export JETTAE_ENV_FILE=.env.live-llm`한 터미널에서 실행해도 결과가 같고 API 키가 테스트에 닿지
않습니다.

```bash
uv run pytest -q                                  # 백엔드 전체(PostgreSQL 전용 테스트는 건너뜀)
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run python scripts/secret_scan.py              # 공개 대상 파일 + 모든 커밋의 비밀값·개인 경로 검사
```

PostgreSQL 테스트는 **지워도 되는 전용 DB**에서만 실행합니다(테스트가 그 DB의 `public` 스키마를 지움).
PostgreSQL 서버가 없으면 `pgserver`로 임시 서버를 띄울 수 있습니다(인자는 임시 폴더):

```bash
uv run --with pgserver python deploy/run_pg_tests.py var/pgtest
```

화면 검사와 E2E(Playwright, API + worker + 화면을 실제 프로세스로 띄움, 폐기용 SQLite, LLM 오프라인·예산 0):

```bash
cd frontend
npm run lint
npm run typecheck        # next typegen && tsc --noEmit (.next 가 없어도 동작)
npm test
npm run build
npm run e2e:install      # chromium 내려받기(한 번)
npm run e2e
```

E2E는 PATH의 `uv`를 씁니다(다른 위치면 `JETTAE_UV`에 경로). CI(`.github/workflows/ci.yml`)는 backend(ruff,
format, mypy, pytest, SQLite 마이그레이션 왕복, 비밀값 검사), postgres(폐기용 PostgreSQL 16 서비스
컨테이너에서 마이그레이션 왕복과 PostgreSQL 테스트), frontend(lint, typecheck, test, build), e2e(Playwright),
docker-build(Compose 설정 검사, API·standalone 웹 이미지 빌드)
작업으로 나뉘며 backend 작업은 gitleaks로 모든 커밋도 검사합니다. 저장소 비밀값을 쓰지 않으며 모든 작업이 `JETTAE_LLM_MODE=offline`, 예산 0으로
실행되고 API 키가 있으면 실패합니다. 의존성 취약점 검사 결과는 `docs/security/dependency-audit.md`.

## 구현 · 검증 · 미검증

아래 표는 이전 단계의 기록입니다. 이번 수정 커밋에서 직접 실행한 검사와 미검증 영역은
[FINAL_VERIFICATION.md](docs/FINAL_VERIFICATION.md)를 기준으로 확인합니다.

"구현"은 코드와 테스트가 있다는 뜻이고, "검증"은 실제로 돌려 본 범위입니다. 테스트 통과는 아래 적은
범위만 뜻하며 운영 보안 전체나 LLM 정확도를 보증하지 않습니다. 명령과 결과는 `docs/PROGRESS.md`.

| 기능 | 구현 | 검증한 범위 | 검증하지 못한 것 |
|---|---|---|---|
| 법정 지급기한·지연일수·지연이자(엔진) | 예 | 단위·속성 테스트, 법령·고시 원문 대조(`law check`), 공정위 의결서 전사 행 E1~E3 | 전사 행은 사람이 검증하지 않음(`verified=no`) |
| 대사(입금 배분, 상태 6종), 선택적 재계산 | 예 | 테스트, E6 재계산 동등성(실제 기록) | 실제 은행 내보내기 원본 |
| 문서 읽기(CSV/XLSX/PDF), XLSX 자원 상한 | 예 | 테스트(행·열·셀·병합·zip·DTD 상한, 루트 이름을 바꾼 공유 문자열·통합 문서 부분, BOM 없는 UTF-16, 스타일·관계·콘텐츠 형식 부분 원소 상한 회귀 포함), 실제 cp949 은행 CSV | 실제 홈택스 엑셀, 100만 셀 근처 정상 파일의 메모리 사용량, 상한 안쪽 최대 크기 파일의 처리 시간 |
| 화면 흐름(업로드→매핑→분석→상세→정정→재승인→보고서) | 예 | Playwright E2E(chromium, 로컬 SQLite) | 다른 브라우저, 여러 탭 동시 사용 |
| HttpOnly 쿠키 세션·CSRF·refresh 회전·로그아웃·회사 격리 | 예 | API 테스트(겹친 갱신 유예, 세션별 갱신 제한, 로그인 실패와 갱신 분리 포함), 화면 단위 테스트(갱신 429·5xx에 로그인 유지), E2E, 로컬 curl | 실제 HTTPS 배포(Secure 쿠키·역프록시 뒤 Origin 검사), 실제 브라우저 여러 탭 |
| 환경 이름·운영 필수 설정 시작 검사 | 예 | 테스트, 잘못된 값으로 실제 실행 | 실제 운영 서버 |
| Agent 조사(화면·worker, single/roles) | 예 | 오프라인·재생·가짜 공급자 테스트, E2E(offline) | 실제 LLM 조사 품질, live 모드 실제 호출 |
| LLM 게이트웨이(예산·재생·동의), VLM OCR | 예 | 가짜 공급자 테스트, 공유 예산 경합(SQLite·PostgreSQL) | 실제 유료 호출 0회, OCR 정확도 |
| MCP 서버 | 예 | 테스트(도구 목록, 토큰별 테넌트, 권한) | 실제 MCP 클라이언트 |
| PostgreSQL | 예 | pgserver PostgreSQL 16(Windows): 마이그레이션 왕복(0006까지)·API 흐름·격리·경합 | 리눅스 운영 서버(CI 작업은 작성했으나 GitHub에서 실행 전) |
| 로컬 데모(`jettae demo run`) | 예 | 테스트, 공개 대상 파일만 복사한 새 폴더에서 실행 | |
| Docker Compose 배포 | 파일만 | 실행하지 않음(Docker 없음) | 이미지 빌드·기동 전체 |
| E4 BPI 2019 | 파이프라인 | 손 예제 | **실행하지 않음**(원본을 받지 못함) |
| 선행문헌 비교(`docs/ip/`) | 틀만 | | 내용 비교 비어 있음 |
