# 공개 이용 조건 (public use)

이 문서는 저장소가 공개되어 있을 때 무엇이 허락되어 있고 무엇이 정해지지 않았는지를 사실대로
적습니다. 법률 자문이 아니며, 아래 내용은 각 출처가 게시한 안내를 옮긴 것입니다.

## 1. 저장소 코드와 문서: 라이선스를 아직 정하지 않았습니다

- 이 저장소에는 `LICENSE` 파일이 없고, 소유자는 아직 공개 라이선스를 고르지 않았습니다.
  `pyproject.toml`의 `license = { text = "Proprietary" }`도 같은 상태를 나타냅니다.
- 라이선스가 없는 저작물은 기본적으로 저작권자가 모든 권리를 가집니다(all rights reserved).
  **저장소가 공개되어 있다는 사실은 복제·수정·재배포·상업적 이용을 허락한다는 뜻이 아닙니다.**
  GitHub에서 공개 저장소를 열람하고 포크하는 것은 GitHub 이용약관이 정한 범위에서만 가능합니다.
- 코드를 쓰고 싶다면 저장소 소유자에게 먼저 문의하세요. 라이선스가 정해지면 `LICENSE` 파일과 이
  문서를 함께 고칩니다. 그 전에는 누구도(코딩 에이전트 포함) 임의로 라이선스 파일을 추가하거나
  바꾸지 않습니다.
- 기여(이슈, 풀 리퀘스트)를 받는 조건도 정해지지 않았습니다.

## 2. 저장소에 포함된 제3자 자료와 출처

저장소에는 공개 자료의 **수집 기록·전사·평가 요약**만 있고, 내려받은 원본(`data/raw/`)과 다시 만들 수
있는 평가 결과 JSON(`data/results/`)은 넣지 않습니다. 원본은 각 사용자가 `jettae sources ...` 명령으로 직접 받습니다. 원본과 그 파생 자료를
이용할 때는 아래 출처의 조건을 따르세요.

| 자료 | 저장소에 있는 것 | 출처와 이용 조건(게시 내용 기준) |
|---|---|---|
| 공정거래위원회 의결서 | `data/manifests/ftc.json`, `ftc_images.json`(URL·해시), `data/seeds/ftc_*.csv`(표 전사, `verified=no`), `data/seeds/ftc_case_facts.jsonl`(본문 수치와 **그 수치 주변의 의결서 본문 발췌**: 238건 의결서의 2,345개 기록, `context` 최대 400자·`raw` 최대 1,021자, 문서 해시·문자 위치 포함) | 국가법령정보센터 Open API(법제처 DRF, `target=ftc`). 국가법령정보 공동활용 이용약관을 따르며, 이용 시 **출처(법제처 국가법령정보센터, 공정거래위원회 의결서)를 표시**합니다. 재배포 조건의 세부는 이 저장소에서 검증하지 않았으므로 원본 파일은 커밋하지 않습니다 |
| 법령·고시 원문 | `data/manifests/law.json`(대조 결과는 `jettae sources law check`로 다시 만듦) | 국가법령정보센터. 수집 기록에는 "저작권법 제7조: 보호받지 못하는 저작물"로 적어 두었습니다. 출처를 표시합니다 |
| 공정위 표준거래계약서 | `data/manifests/contract.json`(추출 결과는 `jettae sources law contract-fetch`로 원본을 받은 뒤 `jettae eval contract`로 다시 만듦. 원본이 없으면 이 명령은 종료 코드 3으로 끝나고 요약을 바꾸지 않음) | 공정거래위원회 누리집. 게시판 안내에 "저작권법 제24조의2(공공저작물의 자유이용)에 따라 자유롭게 이용할 수 있다"고 표시되어 있습니다(2026-10-06 확인). 출처를 표시합니다 |
| BPI Challenge 2019 | `data/manifests/bpi2019.json`(원본은 받지 못함) | 4TU.ResearchData, DOI 10.4121/uuid:d06aff4b-79f0-45e6-8ec8-e19730c248f1, **CC BY 4.0**. 이용 시 저작자·DOI·라이선스를 표시하고 변경 여부를 밝힙니다 |
| npm·Python 의존성 | `frontend/package-lock.json`, `uv.lock`(버전·해시만) | 각 패키지의 라이선스를 따릅니다. 패키지 자체는 저장소에 없습니다 |

`ftc_case_facts.jsonl`은 숫자만이 아니라 공개된 의결서 본문의 짧은 원문 발췌(`context`, `raw`)를
함께 담고 있습니다(합계 약 44만 자). 공개 여부를 정할 때 이 점을 고려하세요. 숫자만 공개하려면 두 필드를
지우거나 줄인 파일을 만들고, 발췌는 각자 `data/raw/`에서 `jettae sources ftc facts`로 다시 만들 수
있습니다. 대표자 이름·주소 같은 개인 정보 형식(대표이사·대표자·주소·소재지)은 검색되지 않았습니다(이름 형식
전체를 검사한 것은 아님).

공정위 의결서 표 전사(`data/seeds/ftc_rows.csv`)는 코딩 에이전트가 표 이미지를 보고 옮긴 것이며
사람이 검증하지 않았습니다. 데모(`jettae demo run`)와 평가 결과에는 이 사실이 함께 표시됩니다. 표의
수치는 피심인이 제출한 자료를 옮긴 것일 수 있습니다.

## 3. 공개 대상과 비공개 대상의 분리

같은 저장소로 로컬 데모, 실제 LLM 실행, 운영 배포를 모두 합니다. **보안·운영 소스 코드는 공개
대상**이고(인증·CSRF·쿠키 세션, 시작 단계 설정 검사, 업로드 자원 상한, 예산·동의 검사, `deploy/`,
`.github/`, `scripts/secret_scan.py`), 공개하지 않는 것은 **값과 자료**뿐입니다. 구분은 `.gitignore`가
정합니다. `scripts/secret_scan.py`는 아래 표의 공개하지 않는 경로 각각을 **경로 규칙**으로도 가지고 있어,
`git add -f`나 `.gitignore` 수정으로 들어온 파일도 찾습니다(테스트가 `.gitignore`의 각 항목이 규칙에
걸리는지 확인). gitleaks(`.gitleaks.toml`)는 파일 내용만 검사합니다.

| 공개 대상(커밋함) | 공개하지 않음(`.gitignore`) | 이유 |
|---|---|---|
| 소스·테스트·마이그레이션·배포·CI 파일 | — | 보안 동작을 누구나 검토하고 재현할 수 있어야 함 |
| 가짜 값만 든 예시 설정(`.env.example`, `.env.live-llm.example`, `.env.prod.example`, `frontend/.env.example`, `deploy/.env.example`) | 실제 `.env`, `.env.*`(예시 제외), `*.pem`·`*.key` 등 | 실제 API 키·JWT 서명 키·DB 비밀번호·본인 법제처 OC |
| — | `var/`(기본 DB `var/jettae.db`, 업로드 저장소 `var/blobs`, Agent 실행 기록, 데모 파일), `*.db`·`*.sqlite*`·`*.dump`, `blobs/`, `llm_cache/`·`llm_replay/`, `llm_budget.jsonl` | 사용자가 올린 자료와 그로부터 만든 DB, 회사 자료가 든 LLM 재생 캐시, 비용 원장 |
| — | `*.log`, `logs/` | 요청·오류 기록에 사용자 정보가 섞일 수 있음 |
| — | `.npmrc`, `.pypirc`, `.netrc`, `.envrc`, `.claude/settings.local.json`, `CLAUDE.local.md` | 개인 도구 설정. 저장소·패키지 인증 정보, 코딩 에이전트의 개인 설정·로컬 경로가 들어갈 수 있음 |
| — | `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`, `.hypothesis/`, 커버리지, `node_modules/`, `.next/`, `next-env.d.ts`, `frontend/test-results/`, `frontend/playwright-report/`, `frontend/.e2e-tmp/` | 기계별 캐시·빌드·테스트 산출물(다시 만들 수 있음, 로컬 경로가 들어 있음) |
| `data/manifests/`(수집 URL·시각·해시·라이선스, 로컬 절대 경로 없음), `data/seeds/`(공정위 의결서 표 전사, 출처 포함) | `data/raw/` | 원본 재배포 조건을 이 저장소에서 검증하지 않음. 원본은 각자 `jettae sources ...`로 받음 |
| `docs/eval_results.md`(평가 요약) | `data/results/` | 전사 자료·수집 기록으로 다시 만들 수 있는 상세 결과. 실행마다 시각·경로가 바뀜 |
| 손으로 쓴 테스트 입력(`tests/`, `frontend/e2e/fixtures/`) | — | 평가 자료가 아님. 테스트용 가짜 값의 예외는 `.gitleaks.toml`과 `scripts/secret_scan.py`의 `ALLOWLIST`에 **규칙 하나·경로 하나·줄 형식 하나**로만 둡니다. 주석 표시로 줄을 예외 처리하는 방법은 없습니다 |

규칙:

- 사용자가 올린 정산서·입금 내역·세금계산서, 그로부터 만든 DB, LLM 재생 캐시, 비용 원장, 로그는
  **절대 커밋하지 않습니다**.
- 실제 비밀값은 `.env`·서버 비밀 저장소에만 둡니다. 예시 파일에는 `change-me-...` 같은 가짜 값만 둡니다.
- 저장소 안 문서·설정에는 개인 절대 경로(사용자 폴더 이름 등)·개인 이메일·전화번호를 쓰지 않습니다.
  경로는 저장소 루트 기준 상대 경로나 환경 변수(`$HOME`, `JETTAE_STATE_DIR`)로 적습니다.
- 커밋 전과 CI에서 `python scripts/secret_scan.py`가 공개 대상 파일과 모든 커밋 이력을 검사합니다.
  이 검사는 알려진 형식의 키·비밀값 대입·URL 속 비밀번호·개인 절대 경로·금지 경로(DB, env, 로그, 캐시,
  업로드 저장소, LLM 재생 캐시·비용 원장, 원본 다운로드, 평가 결과, 개인 도구 설정)를 찾으며, 찾은 값은
  출력하지 않습니다. 공개 전에는 gitleaks
  (`gitleaks git --redact`, `gitleaks dir --redact`)로도 검사합니다. 어느 검사도 모든 비밀값을 찾는다는
  보장은 없습니다.
- 이미 커밋된 비밀값은 파일을 지워도 이력에 남습니다. 그런 경우 먼저 그 값을 폐기(재발급)하고
  이력을 정리한 뒤 공개합니다.

## 4. 결과의 성격

이 도구의 출력은 문서와 입금의 차이, 필요한 서류, 확인이 필요한 조건, 관련 근거입니다. 법적 판단이나
권리 확정이 아니며, 공개 의결서로 한 검산 결과(`docs/eval_results.md`)도 그 자료 범위에서의 일치·차이만
뜻합니다.
