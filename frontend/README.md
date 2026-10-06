# 제때받기 웹 화면 (frontend/)

Next.js 16 (App Router, TypeScript), npm, Node.js 22.18 이상(`package.json` 의 `engines`).
모든 화면은 같은 출처의 `/api/v1/*` 를 부르고, Next rewrites 가 백엔드(`src/jettae/api`)로 넘긴다.

## 명령 (이 순서로 깨끗한 상태에서 통과해야 한다)

```bash
cd frontend
npm ci                # package-lock.json 그대로 설치
npm run lint          # eslint (next core-web-vitals + typescript)
npm run typecheck     # next typegen && tsc --noEmit  (.next 가 없어도 라우트 타입을 먼저 만든다)
npm test              # node --test: src/lib/lib.test.ts, src/lib/client.test.ts (Node 의 타입 제거 실행)
npm run build         # 프로덕션 빌드(.next). JETTAE_API_ORIGIN 이 이때 고정된다
npm run start         # 빌드 결과 실행(기본 3000)
```

`npm run typecheck` 가 `next typegen` 을 먼저 부르는 이유: `PageProps`·`LayoutProps` 같은 라우트 타입과
`next-env.d.ts` 는 Next 가 만드는 파일(.gitignore 대상)이라, 새로 받은 저장소에서 `tsc` 만 돌리면 실패한다.

## E2E (Playwright, chromium)

```bash
# 저장소 루트에서 백엔드 환경을 먼저 만든다
uv sync --frozen --all-extras
# frontend/ 에서
npm ci
npm run e2e:install   # chromium 내려받기(한 번)
npm run e2e           # API+worker+화면을 띄우고 e2e/*.spec.ts 실행
```

- `playwright.config.ts` 의 webServer 두 개가 프로세스를 띄운다.
  - `e2e/backend.mjs`: `JETTAE_ENV=test`, `frontend/.e2e-tmp/backend/e2e.db`(실행마다 새로 만듦)에
    `jettae api migrate` 후 `jettae api serve` 와 `jettae worker run`. 개발 셸의 `JETTAE_*`·`*_API_KEY` 는
    물려받지 않고 LLM 은 오프라인(`JETTAE_LLM_MODE=offline`, 예산 0). JWT 서명 키는 실행마다 임의로 만든다.
    파일 저장소는 OS 임시 폴더(`jettae-e2e-<포트>`)에 둔다(긴 경로를 못 쓰는 Windows 의 Python 때문).
  - `e2e/web.mjs`: E2E API 주소로 `.e2e-tmp/next` 에 따로 빌드한 뒤 `next start`. 운영 빌드(.next)와
    `tsconfig.json` 은 건드리지 않는다(빌드는 `tsconfig.e2e.json` 사용).
- 포트: `E2E_API_PORT`(기본 18000), `E2E_WEB_PORT`(기본 13000). 이미 쓰는 포트면 시작하지 않는다.
- uv 위치: `JETTAE_UV` 또는 `UV` 환경 변수, 없으면 PATH 의 `uv`.
- 입력: `e2e/fixtures/*.csv` (손으로 쓴 두 줄짜리 테스트 입력, 평가 데이터 아님).
- 확인하는 것: 가입 → 업로드 → 열 매핑 확인 → 분석 → 결과·상세(증빙·원문 위치·필요 서류) → 오프라인
  Agent 조사(필요 서류, 결과 해시 불변) → 승인 → 정정본 업로드 → `REVIEW_REQUIRED` → 재승인(CSRF 헤더) →
  보고서 내려받기; 쿠키 HttpOnly 속성, CSRF 헤더 없는/틀린 요청 403, 로그아웃 후 보호 화면이 로그인으로 이동.
- 확인하지 않는 것: 실제 LLM 호출, 운영 배포(https·Secure 쿠키·역프록시), 여러 탭·여러 브라우저 동시 사용.

## 설정 (`frontend/.env.local`, 예시는 `.env.example`)

Next.js 는 `frontend/.env.local` 을 읽는다. 아래 값은 **`next build` 때 고정**되므로 바꾸면 다시 빌드한다.

| 변수 | 기본 | 설명 |
|---|---|---|
| `JETTAE_API_ORIGIN` | `http://127.0.0.1:8000` | rewrites 대상(백엔드 주소). `next start` 때만 바꾸면 반영되지 않는다 |
| `NEXT_PUBLIC_MAX_UPLOAD_MB` | `20` | 업로드 사전 검사 크기. 서버 `JETTAE_MAX_UPLOAD_BYTES` 와 맞춘다 |

브라우저가 다른 출처의 API 를 직접 부르는 방식(이전의 `NEXT_PUBLIC_API_BASE`)은 없앴다. 인증 쿠키와
CSRF 쿠키가 화면 출처에 있어야 화면 스크립트가 CSRF 토큰을 읽어 보낼 수 있기 때문이다. 운영에서
화면과 API 를 한 도메인 아래 역프록시로 묶을 때도 `/api/` 경로는 백엔드로, 나머지는 Next 로 보낸다.

## 로그인 (HttpOnly 쿠키)

- 가입·로그인 응답으로 서버가 쿠키를 심는다: `jt_access`(HttpOnly, Path=/api), `jt_refresh`(HttpOnly,
  Path=/api/v1/auth), `jt_csrf`(스크립트가 읽는 CSRF 토큰). 화면은 토큰을 읽지도 저장하지도 않는다
  (localStorage·sessionStorage 사용 없음, Authorization 헤더 없음). 표시용 사용자·회사·역할만
  `GET /api/v1/auth/me` 로 받아 메모리에 둔다.
- POST/PUT/PATCH/DELETE 에는 `jt_csrf` 값을 `X-CSRF-Token` 헤더로 보낸다. 없거나 다르면 서버가 403
  `csrf_failed`.
- 401 이면 `POST /api/v1/auth/refresh` 를 한 번만 부르고(동시 요청은 한 번을 공유) 원래 요청을 한 번 다시
  보낸다. refresh 가 401/403 이면(세션이 끝남) 로그인 상태를 지우고 보호 화면은 `/login?next=...` 로
  이동한다. 429·5xx·네트워크 오류는 세션이 끝났다는 뜻이 아니므로 로그인 상태를 지우지 않고 "error"
  (서버 연결 문제)로 둔다. 여러 탭이 같은 refresh 쿠키로 거의 동시에 갱신하는 경우는 서버의 짧은 재사용
  유예(`JETTAE_REFRESH_REUSE_GRACE_S`, 기본 20초)가 받는다.
- 로그아웃은 `POST /api/v1/auth/logout`(서버가 refresh 토큰 묶음을 폐기하고 세 쿠키를 지움). 서버 호출이
  실패해도 화면 상태는 지운다. 이 경우 access 쿠키는 만료(기본 15분)까지 남는다.
- 화면의 로그인 확인은 편의 기능이다. 접근 통제와 회사(테넌트) 결정은 서버가 쿠키로 한다.

## 화면

| 경로 | 내용 |
|---|---|
| `/login` | 로그인 · 가입(새 회사). 쿠키 세션, 상단에 회사 이름 표시 |
| `/upload` | 끌어다 놓기 업로드, 형식·크기 안내, 문서 목록·상태 |
| `/mapping?dv=` | 제안 매핑 표(신뢰도 구간·이유), CSV 미리보기, 열 수정, 거래처 등 옵션, 확정 → 다시 읽기 작업 |
| `/analysis` | 분석 실행(as_of, 휴일 이월, 원 단위 처리) · 최근 작업 |
| `/job?id=` | 작업 상태 폴링(1초→최대 5초), 취소, 종류별 결과 |
| `/results` | 거래별 상태 배지, 금액·배분·차액, 변형별 지급기한·지연일수·지연이자, 가정, 확인할 조건, 필요 서류 |
| `/decision?id=` | 사실과 원문 위치, 규칙·출처 링크, 검사, 이력, 확인(승인), **Agent 조사** |
| `/changes` | 정정·추가 자료 업로드 → 영향 범위 + 이전/현재 비교 |
| `/report` | CSV/HTML 내보내기, '확인된 결과만' 409 처리 |
| `/check` | 무료 검산(로그인 없음) → `POST /api/v1/public/due` |

결정 ID에 `:`·`#` 가 들어 있어 경로 대신 쿼리(`?id=`)로 넘기고, API 경로에는 항상 `encodeURIComponent` 한다.

### Agent 조사 (`/decision?id=`)

- 방식 `single`(단일 Agent) 또는 `roles`(역할 분담), 실행 모드 `offline`(기본, 규칙 기반 계획·LLM 호출 없음),
  `replay`(서버가 허용할 때), `live`(서버 `GET /api/v1/investigations/capabilities` 의 `live_enabled` 가
  true 일 때만 보임). 서버 예산 설정이 허용하지 않으면 조사는 `refused` 로 끝나며 유료 호출은 없다.
- `POST /api/v1/decisions/{id}/investigations` → worker 의 `investigate_decision` 작업. 화면은 목록을
  1초→최대 5초 간격으로 다시 읽는다.
- 발견 사항(필요 서류, 인용 원문 위치·발췌), 사용량(단계·도구 호출·토큰·비용 원)을 보여 준다.
- 조사 결과는 참고 정보이며 엔진이 계산한 금액·지급기한·지연일수·지연이자를 바꾸지 않는다(E2E 가 조사 전후
  결과 해시가 같은지 확인). 실제 LLM 조사 품질은 검증하지 않았다.

## 표현 원칙

- 처리 실패(빨강) / 자료 없음(회색) / 판단 보류(노랑)를 서로 다른 메시지로 보여 준다. 스캔 PDF·손상 파일은 "거래 없음"이 아니라 처리 실패다.
- 화면 하단에 화면 버전·서버 API 버전(openapi `info.version`)·시간대를 표시하고, 결과에는 as_of·규칙 버전·결과 해시를 표시한다.
- 법적 결론을 나타내는 말은 쓰지 않는다. `npm test` 가 소스 전체와 표시 문구에서 금지어(백엔드 `FORBIDDEN_PHRASES` + 추가)를 검사한다.

## `POST /api/v1/public/due`

인증 없이 호출. 요청 `{trade_type: "direct"|"consignment"|"subcontract", base_date, paid_date, as_of, amount(int 원), rollover: bool|null, rounding: "floor"|"half_up"}`.
응답은 `rules.kr_retail.compute_due(...)` 결과를 `to_plain`(또는 CLI `--json` 의 정규 형식) 그대로 — 화면이 두 형식 모두 읽는다(`src/lib/due.ts`). 기준일이 없으면 `Insufficient`(missing, required_documents, notes). 서버가 404/405를 주면 화면은 "처리 실패: 서버 미제공"을 보여 주며 브라우저에서 계산을 흉내 내지 않는다(휴일 달력·규칙 버전은 서버에만 있음). 공개 엔드포인트이므로 IP별 분당 요청 수를 제한한다(`JETTAE_PUBLIC_IP_PER_MINUTE`, 기본 30, 초과 시 429). 응답에는 `result: "due" | "insufficient"` 가 함께 온다.
