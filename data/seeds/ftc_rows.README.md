# ftc_rows.csv / ftc_tables.csv — rows transcribed from 공정위 의결서 table images

Real data, no synthesis. Every row was read from a table image published with a 공정거래위원회
decision (법제처 DRF Open API, `target=ftc`) and typed in by `claude-agent` (the coding agent),
by opening each PNG and enlarged crops of it. **Nothing has been checked by a person
(`verified=no`).** Values that could not be read are left blank and explained in `notes`.

## Images used (8 images, 4 decisions)

| decision_id | case_no | decision | table | flSeq | image | rows | notes |
|---|---|---|---|---|---|---|---|
| 19065 | 2024유대1559 | 홈플러스(주) 대규모유통업법 (2026.2.10.) | 표 11 직매입 관련 상품대금 지연지급 내역 | 163492277 | 645x500 | 14 | no base-date column; rows of 연번 4..457 elided (⋮) |
| 19065 | 2024유대1559 | 〃 | 표 12 특약매입, 임대을 관련 상품판매대금 지연지급 내역 | 163492279 | 645x437 | 9 | no base-date column; 연번 465..530 elided; no unit line printed |
| 19065 | 2024유대1559 | 〃 | 표 14 상품판매대금 지연지급(공탁) 내역 | 163492283 | 648x596 | 22 | 월판매마감일 given; 연번 5..20 elided |
| 19147 | 2024유대1554 | 롯데쇼핑(주) 대규모유통업법 (2026.3.6.) | 표 8 직매입 상품판매대금 지연지급 및 지연이자 내역 | 164088685 | 647x529 | 9 | aggregated rows: 상품 수령기간 a~b → 법정 지급기한 a~b; 연번 3..18 elided |
| 19147 | 2024유대1554 | 〃 | 표 9 위수탁ㆍ특약매입 상품판매대금 지연지급 및 지연이자 내역 | 164088687 | 649x624 | 15 | no due-date column; 연번 4..56 elided |
| 19299 | 2025신하0954 | 그랑몬스터(주) 불공정하도급거래행위 (2026.7.24.) | 표 11 지연이자 미지급 내역 | 167807599 | 639x454 | 8 | complete table; amounts in 천 원 |
| 16947 | 2021유통1898 | ㈜이마트 대규모유통업법 (2023.9.8.) | 표 5 상품판매대금 지연지급(공탁) 내역 | 133422117 | 652x300 | 7 | complete; **low resolution** — every row marked `UNCERTAIN` |
| 16947 | 2021유통1898 | 〃 | 표 6 지연이자 미지급 내역 | 133422119 | 652x394 | 11 | complete; **low resolution** — every row marked `UNCERTAIN` |

Image URL: `https://www.law.go.kr/LSW/flDownload.do?flSeq=<flSeq>`; sha256 and fetch time of
each image are in `data/manifests/ftc_images.json` (the same files are produced by
`jettae sources ftc images`; the images themselves are not committed — `data/raw/` is
gitignored). Decision XML: `data/manifests/ftc.json`.

The 쿠팡 decision (19005) was checked: its delay table (`<표 56>`, flSeq 162805909) is a single
aggregated line (masked counts, delay 1~233 days, interest totals), not row-level, so it is not
transcribed; its text figures are in `ftc_case_facts.jsonl`.

## Conventions

- One CSV row per visible table row, in the order printed (`row_idx` 1..n per image). Rows hidden
  behind "⋮" are not represented; `notes` says where the elision is. `serial_no` = the printed 연번.
- Dates are normalised to ISO `YYYY-MM-DD` (the tables print `22. 11. 21.`, `2024. 5. 10.`,
  `2021.08.31`); numbers are written without thousands separators. Nothing else is changed —
  apparent typos are kept as printed (e.g. 19065 표 14 row 12: 법정지급기한 printed
  `2024. 5. 10.` for 월판매마감일 2023-03-31; checked on a 3x crop).
- `principal_krw`, `interest_in_table`, `unpaid_interest_in_table` hold the printed number. When
  `unit_note` is `천원` the printed number is in thousands of KRW (19299).
- `deal_type`: `direct` (직매입), `consignment` (특약매입·위수탁·임대을·매장임차인), `subcontract`.
- `base_date_kind` / `base_date`: only when the table prints the statutory base date
  (`sales_close_date` = 월 판매마감일, `goods_received_date` = 상품 수령(기간),
  `object_received_date` = 목적물수령일). 19299's column is headed
  `목적물수령일(세금계산서 발행일)` — the decision equates the two; recorded as printed.
  Empty when the table has no base-date column (19065 표 11/12) → these rows test abstention (E3).
- `due_date_in_table`: the table's due column. Its meaning is per table in `ftc_tables.csv`
  (`due_col_semantics`): `due_date` = 법정지급기한, `delay_start` = 기산일 (the first day of delay,
  i.e. due date + 1; 16947 각주 8, 19299).
- Range rows (19147 표 8) keep `a~b` in `base_date`, `due_date_in_table` and
  `delay_days_in_table`; `principal_krw` there is the aggregated 지연건의 매입총액 and `notes`
  holds 지연건수.
- `UNCERTAIN` in `notes` marks rows with digits that are hard to distinguish at the source
  resolution (3/8/9/0 in 16947). The evaluation reports results with and without these rows.
  Month-end plausibility was used to read `09.30` vs `08.30` (noted per row); the rule engine was
  **not** used to choose readings.
- Extra columns not in the requested schema: `serial_no` (rows CSV). `ftc_tables.csv` holds
  per-table metadata copied from the same images (headers, unit, due-column meaning, whether rows
  are elided, printed 합계 values, whether 연번 counts suppliers or transactions).

## Transcription self-checks (no engine involved)

`jettae eval ftc` sums transcribed columns of complete tables and compares them with the printed
합계 (see `docs/eval_results.md`, "Transcription QA"). At the last run: 19299 표 11 principal and
interest sums equal the printed totals; 16947 표 5 interest sum equals the printed total but the
principal sum differs by 700 KRW (at least one low-resolution principal digit is probably
misread); 16947 표 6 interest sum equals the printed total.

## Case-level facts

`ftc_case_facts.jsonl` is produced by `jettae sources ftc fetch` / `jettae sources ftc facts`
from the decision text (regex, `extractor=ftc.regex.v1`), one JSON object per fact with
`decision_id`, `section`, `char_start`, `char_end` (offsets into that XML element's text),
`raw`, `confidence`, `masked` (numbers shown as `*` are never guessed: `value=null`) and
`doc_sha256` of the XML it came from.
