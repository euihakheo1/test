# 평가 절차 (eval protocol)

SPEC §6의 평가를 어떤 입력으로, 어떤 명령으로, 무엇을 세는지 적는다. 결과 숫자는 이 문서에 쓰지
않는다. 숫자는 실행이 남긴 `data/results/*.json`과 `docs/eval_results.md`(실행마다 해당 블록을
교체)에만 있다. 결과 파일이 없으면 숫자를 주장하지 않는다. `data/results/`는 다시 만들 수 있는
산출물이라 저장소에 넣지 않으며(`.gitignore`), 공개 저장소에서 확인할 수 있는 숫자는
`docs/eval_results.md`의 요약이다. 상세 JSON이 필요하면 아래 재현 순서로 다시 만든다.

## 공통 규칙

1. **실제 공개 자료만** 쓴다. `tests/` 아래 손 예제(fixture)는 코드 정확성 확인용이며 평가 결과에
   섞지 않는다. 합성 데이터셋은 쓰지 않는다.
2. 입력마다 출처를 남긴다: `data/manifests/*.json`(URL, 수집 시각, sha256, 라이선스, 실패 기록),
   전사 자료는 `data/seeds/*.README.md`(이미지 flSeq, 표 번호, 행 번호, 전사자, `verified`).
3. 실행 기록에 코드·데이터·규칙 버전과 해시, 설정, 명령을 남긴다(각 결과 JSON의 `run`/`engine`
   항목). LLM을 쓰는 평가는 모델·프롬프트 해시와 비용을 남긴다(현재 유료 호출 0회).
4. 받지 못한 자료는 "Not run"으로 기록하고 재실행 명령을 적는다(E4 참고).
5. 추적 대상이 아닌 입력(직접 고른 파일)으로 돌린 결과는 "AD-HOC INPUT"으로 따로 저장하고
   `docs/eval_results.md`에 넣지 않는다(`eval recompute`). 검토자가 추적 파일을 건드리지 않고
   재현하려면 `eval ftc --out <json> --md <md>`처럼 출력 경로를 바꾼다.
6. 결과는 "엔진 계산과 공개 문서 수치의 일치/차이"를 말할 뿐, 법적 판단이 아니다.

## E1 사건 단위 검산 — `uv run jettae eval ftc`

코드: `src/jettae/evals/ftc_eval.py`(실행·출력) — `ftc_inputs.py`(입력), `ftc_rows.py`(행 단위 검사),
`ftc_aggregate.py`(집계·E1), `ftc_report.py`(마크다운). 결과 JSON의 `engine.code_sha256`은 이 다섯
파일과 규칙·날짜·금액 코드의 해시다.

- 입력: `data/seeds/ftc_case_facts.jsonl`(의결서 본문에서 정규식으로 뽑은 합계·범위·건수, 원문 위치
  포함), `data/seeds/ftc_rows.csv` + `ftc_tables.csv`(별지 표 전사).
- 계산: 본문 수치와 표 인쇄 합계 비교, 연번으로 본 업체·거래 수 비교, 표의 모든 행이 전사된
  경우에만 엔진으로 지연이자 합계를 다시 계산(variant별).
- 하지 않는 것: 행이 생략(⋮)된 표는 엔진 합계를 내지 않는다.
- 이율의 출처(지표 정의): 지연기간에 해당하는 이율 고시 버전이 규칙 레지스트리에 없는 행은 엔진의
  지연일수에 **같은 의결서 본문에 적힌 이율**을 곱해 계산한다. 이 합계는 검산 대상 본문에서 이율을
  가져오므로 독립적인 엔진 결과가 아니다(부분적으로 순환). 결과 JSON의 E1 항목은
  `interest_source`(`registry` / `stated_rate` / `mixed`)와 `rows_registry_rate`·`rows_stated_rate`를,
  요약은 `engine_computable_by_source`를 가지며, 마크다운 표는 각 합계 옆에 출처를 적는다.
- 마크다운 머리말은 실제로 쓴 결과 JSON 경로(`--out`)를 적는다.

## E2 행 단위 검산 — 같은 명령

- 입력: 기준일이 있는 전사 행.
- 계산 variant: rollover(on/off) x 원 단위(floor/half_up) 4개.
- **검사 가능(evaluable)**: 한 검사(지급기한·지연일수·지연이자)는 엔진 값과 표 값이 둘 다 있을 때만
  채점한다. 둘 중 하나라도 없으면 그 검사는 `None`(채점 불가)이고, **일치로 세지 않는다**.
- 지표(variant별, 결과 JSON 키):
  - `due_ok` / `delay_ok` / `interest_ok`: 지급기한 정확 일치, 지연일수 정확 일치, 지연이자 허용
    오차(±1원, 표 단위가 천원이면 ±1,000원) 일치. 분모는 그 검사가 가능한 행.
  - `all_three_ok` ("세 가지 모두 일치"): 세 검사가 **모두 채점 가능하고 모두 일치**한 행. 분모는
    E2 전체 행(표에 값이 없거나 엔진이 계산하지 않은 행은 불일치로 셈).
    `all_three_ok_of_fully_evaluable`은 같은 분자를 세 검사가 모두 가능한 행으로 나눈 값.
  - `all_available_ok` ("가능한 검사 모두 일치"): 채점 가능한 검사(1개 이상)가 모두 일치. 더 약한
    지표이며 "세 가지 모두"라고 부르지 않는다.
  - `fully_evaluable`: 세 검사가 모두 채점 가능한 행의 비율.
  - `engine_full_computation`: 엔진이 지급기한·지연일수·지연이자를 모두 계산한 행의 비율(표 값과
    무관). 행 단위 값은 네 variant 모두에서 계산한 행.
- 행 단위 보조 지표: `abstained`(기준일이 있는데 엔진이 `Insufficient`로 보류한 비율),
  `evaluable_checks_per_row`(행마다 채점 가능한 검사 수 0~3의 분포),
  `evaluable_rows_per_check`(검사별 채점 가능한 행 수).
- **제시한 계산 중 정답을 포함한 비율** (`post_hoc_any_variant`, 사후 지표): 4개 variant 중 하나라도
  일치하면 그 행을 센다. 정답을 본 뒤 variant를 고르는 셈이므로 **상한값**이며, 제품이 미리 정한
  하나의 설정의 정확도가 아니다. 예전 보고서의 "any variant / 어느 variant든"은 이 지표이고, 그때의
  "all three"는 실제로는 `all_available_ok`(채점 불가 검사를 통과로 취급)였다.
- 보조: E2-cond(기준일 없는 행을 표가 적은 지급기한으로 다시 계산), 범위 행(기간 a~b의 양 끝 기한;
  양 끝 지급기한만 비교하며, rollover 둘 중 하나라도 맞으면 세는 값도 사후 지표로 표시).
- 주의: 전사는 `verified=no`. 표 수치는 피심인 제출 자료를 옮긴 것일 수 있다. 등록된 지연이율 고시
  버전이 없는 지연기간 행은 엔진이 이자를 내지 않으며, 의결서 본문 이율로 한 계산은 따로 보고한다.

## E3 기준일 보류 — 같은 명령

- 기준일 없는 행에서 엔진이 `Insufficient`로 계산을 보류했는지, 필요 서류를 안내했는지.
- 기준일 있는 행 중 보류한 행과 그 이유(규칙 버전 미등록 등)를 따로 센다.

## E4 실제 P2P 흐름(BPI Challenge 2019) — `uv run jettae sources bpi2019 stats` (= `jettae eval bpi`)

- 입력: 4TU에서 받은 `BPI_Challenge_2019.xes`(md5·크기 검증) 또는 그 변환 파일만.
  `fetch --file`로 등록한 파일은 4TU 게시 md5(또는 `--md5`)와 대조하며, 대조하지 못하면
  `unverified_local` 상태가 되고 결과 머리에 출처 미확인 경고가 붙는다. md5가 다르면 등록을 거부한다.
- 지표(정답 라벨이 없으므로 **정확도가 아님**): 입고(GR)·송장(IR)·지급(Clear) 연결 상태 수, 금액
  불일치 비율, IR/GR 기준 지급 소요일 분포(nearest-rank 백분위), 처리 시간, 품목 분류별.
- 국내 법정 기한을 적용하지 않는다.
- 현재 상태: 4TU 점검으로 원본을 받지 못해 **실행하지 않음**.

## E5 약정 조건 추출 — `uv run jettae sources law contract-extract` (= `jettae eval contract`)

- 입력: 공정위 누리집의 표준거래계약서(직매입·특약매입·위수탁) 원본(HWP/HWPX/PDF).
- 지표: 지급 조항 추출 여부, 기산점 표현이 규칙 레지스트리의 기준과 같은지, 기한 일수(공란 "(  )일"
  포함), 작성 안내(※)의 일수. 독립 정답 라벨이 없으므로 일관성 검사와 키워드 범위 자체 점검만 한다.
- 표준계약서 사용 여부로 위반을 판단하지 않는다.

## E6 재계산 동등성 — `uv run jettae eval recompute`

- 입력: `data/seeds/ftc_rows.csv`(실제 의결서 전사 행)와, 있으면 등록된 BPI 2019 로그의 앞부분.
- 절차: 미수금을 기본 스냅샷으로 두고 실제 지급일 순서로 입금(및 정정 행)을 하나씩 적용한다. 매 단계
  선택적 재계산(이전 결과에서 이어감)과 같은 스냅샷의 전체 재계산을 비교한다(결정, 그룹, 미귀속 입금,
  의존성 그래프).
- 지표: 불일치 단계 수(목표 0), 전체 재계산으로 돌아간 단계 수, 재계산한 결정 수 / 전체 결정 수, 시간.
- 기준일(as_of)은 시나리오의 마지막 입금일로 고정한다(as_of 이후 입금은 분석에서 제외되는 규칙과 충돌하지
  않도록).

## 재현 순서(새 환경)

저장소의 전사 자료(`data/seeds/`)만으로 E1~E3(`eval ftc`)과 E6(`eval recompute`)이 실행된다(공개 대상 파일만
복사한 새 폴더에서 2026-10-06 확인). E4·E5는 원본을 먼저 받아야 한다. `eval ftc`는 `--out`·`--md`를 주지
않으면, 나머지 평가는 항상 `data/results/*.json`(추적 안 함)과 추적 파일 `docs/eval_results.md`를 갱신한다.

```bash
uv sync --frozen --all-extras
uv run jettae eval ftc --out var/ftc_eval.json --md var/ftc_eval.md   # 추적 파일을 건드리지 않는 확인
uv run jettae sources law fetch && uv run jettae sources law check
uv run jettae sources law contract-fetch && uv run jettae eval contract
uv run jettae sources ftc fetch && uv run jettae sources ftc images && uv run jettae sources ftc facts
uv run jettae eval ftc
uv run jettae eval recompute
uv run jettae sources bpi2019 fetch && uv run jettae sources bpi2019 convert && uv run jettae sources bpi2019 stats
```

법제처 DRF는 샘플 키 `OC=test`로 개발했다. 운영·대량 수집은 `JETTAE_DRF_OC`에 본인 키를 넣는다
(`JETTAE_ENV=prod`에서는 `test`나 예시 값이면 `sources ftc/law`가 시작하지 않는다). DRF 자료의 재배포
조건은 확인하지 않았으므로 `data/raw/`는 저장소에 넣지 않는다(출처 표시는 `docs/PUBLIC_USE.md`).
