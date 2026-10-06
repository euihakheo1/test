/**
 * 순수 함수 단위 테스트(node --test, 타입 제거 실행). 손으로 쓴 작은 예제만 쓴다(평가 데이터 아님).
 * 응답 예시는 2026-10-06 백엔드(/api/v1)와 `jettae rules due --json` 실제 출력 모양을 옮긴 것이다.
 */
import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import { parseCsvPreview } from "./csv.ts";
import { normalizePublicDue, validateDueInput } from "./due.ts";
import {
  DOC_KIND_LABEL,
  DOC_STATUS_LABEL,
  FORBIDDEN_PHRASES,
  MISSING_LABEL,
  REVIEW_LABEL,
  STATUS_HELP,
  STATUS_LABEL,
  UNRESOLVED_LABEL,
  checkUploadFile,
  decodeText,
  describeLocator,
  docStatusMessage,
  forbiddenIn,
  isIsoDate,
  parseWon,
  sliceContext,
  won,
} from "./fmt.ts";
import { buildMapping, duplicateColumns, initialSelection, missingRequired } from "./mapping.ts";
import {
  APPLY_STATE_LABEL,
  applyMsgKind,
  applyStateLabel,
  applyTitle,
  approveErrorView,
  canAcknowledge,
  carriedOverText,
  countsText,
  mismatchedTotals,
} from "./apply.ts";
import {
  compare,
  contractInfo,
  evidenceInfo,
  keyNumbers,
  pendingText,
  snapshotOf,
  subjectLabel,
  type Snapshot,
} from "./view.ts";
import { ApiError } from "./api.ts";
import type { ApplyOutcome, DecisionDetail, MappingOut } from "./api.ts";

const M = (amount: number) => ({ amount, currency: "KRW" });

test("won formats integer KRW and refuses to pretend non-integers are won", () => {
  assert.equal(won(M(10_000_000)), "10,000,000원");
  assert.equal(won(null), "-");
  assert.equal(won({ amount: 1.5, currency: "KRW" }), "1.5 KRW");
});

test("parseWon accepts grouped digits only", () => {
  assert.equal(parseWon("10,000,000"), 10_000_000);
  assert.equal(parseWon(" 5000원 "), 5000);
  assert.equal(parseWon("1.5"), null);
  assert.equal(parseWon("-3"), null);
  assert.equal(parseWon(""), null);
});

test("isIsoDate rejects impossible dates", () => {
  assert.ok(isIsoDate("2025-02-28"));
  assert.ok(!isIsoDate("2025-02-30"));
  assert.ok(!isIsoDate("2025-2-3"));
});

test("describeLocator: CSV, XLSX and PDF locators", () => {
  assert.equal(
    describeLocator({ table_name: "csv", row: 2, col: 5, col_letter: "E", header: "정산금액", char_start: 51, char_end: 59 }),
    "2행 · E열 · (정산금액) · 문자 51–59",
  );
  assert.equal(describeLocator({ sheet: "Sheet1", cell: "D12", header: "금액" }), "시트 'Sheet1' · 셀 D12 · (금액)");
  assert.equal(
    describeLocator({ page: 3, table: 1, row: 4, char_start: 10, char_end: 20 }),
    "3쪽 · 표 1 · 4행 · 문자 10–20",
  );
  assert.equal(describeLocator(null), "위치 정보 없음");
});

test("sliceContext highlights the span and flags excerpt mismatches", () => {
  const text = "정산번호,거래형태,상품수령일,판매마감일,정산금액,거래처\nA-1,직매입,2025-08-07,,10000000,가나유통\n";
  const ok = sliceContext(text, 51, 59, "10000000");
  assert.ok(ok);
  assert.equal(ok.hit, "10000000");
  assert.ok(ok.matches);
  assert.equal(ok.before, "A-1,직매입,2025-08-07,,");
  assert.equal(ok.after, ",가나유통");
  assert.equal(sliceContext(text, 51, 59, "999")?.matches, false);
  assert.equal(sliceContext(text, 50, 9999, "x"), null);
});

test("decodeText: utf-8 BOM stripped, cp949 fallback", () => {
  const utf = new TextEncoder().encode("﻿날짜,금액");
  assert.deepEqual(decodeText(utf.buffer as ArrayBuffer), { text: "날짜,금액", encoding: "utf-8" });
  // "가나" in cp949 = b0 a1 b3 aa
  const cp = new Uint8Array([0xb0, 0xa1, 0xb3, 0xaa]);
  assert.deepEqual(decodeText(cp.buffer), { text: "가나", encoding: "cp949" });
});

test("document status messages distinguish failure / pending / empty-not-implied", () => {
  assert.equal(docStatusMessage("UNSUPPORTED_SCAN").kind, "failure");
  assert.match(docStatusMessage("UNSUPPORTED_SCAN").text, /거래가 없다는 뜻이 아닙니다/);
  assert.equal(docStatusMessage("CORRUPT", "bad zip").kind, "failure");
  assert.equal(docStatusMessage("FAILED").kind, "failure");
  assert.equal(docStatusMessage("NEEDS_MAPPING").kind, "pending");
  assert.equal(docStatusMessage("PARSED").kind, "ok");
});

test("checkUploadFile: extension and size", () => {
  assert.equal(checkUploadFile("a.csv", 10, 100), null);
  assert.match(checkUploadFile("a.xlsm", 10, 100) ?? "", /지원하지 않는 형식/);
  assert.match(checkUploadFile("a.pdf", 1000, 100) ?? "", /너무 큽니다/);
  assert.match(checkUploadFile("a.csv", 0, 100) ?? "", /빈 파일/);
});

test("parseCsvPreview handles quotes, CRLF and tabs", () => {
  assert.deepEqual(parseCsvPreview('a,b\r\n"1,000","x""y"\n'), [["a", "b"], ["1,000", 'x"y']]);
  assert.deepEqual(parseCsvPreview("a\tb\n1\t2"), [["a", "b"], ["1", "2"]]);
  assert.equal(parseCsvPreview("1\n2\n3\n4", 2).length, 2);
});

// ------------------------------------------------------------------ mapping
const MAP: MappingOut = {
  doc_version_id: "docv_x",
  document_status: "NEEDS_MAPPING",
  suggestion: {
    format_id: "kr_bank_txn",
    matches: [
      { field: "description", column: 2, header: "메모", confidence: "high", reason: "" },
      { field: "memo", column: null, header: null, confidence: "none", reason: "" },
    ],
    headers: ["날", "돈", "메모"],
    fields: [
      { name: "txn_date", label: "거래일자", kind: "date", required: true },
      { name: "deposit", label: "입금액", kind: "amount", required: false },
      { name: "description", label: "내용", kind: "text", required: false },
      { name: "memo", label: "적요", kind: "text", required: false },
    ],
  },
  confirmed: null,
};

test("mapping: suggestion -> selection -> MappingRequest (columns + options)", () => {
  const init = initialSelection(MAP);
  assert.deepEqual(init.selection, { txn_date: null, deposit: null, description: 2, memo: null });
  assert.deepEqual(init.options, {});
  assert.equal(init.formatId, "kr_bank_txn");
  assert.deepEqual(missingRequired(MAP, init.selection), ["txn_date"]);
  const sel = { ...init.selection, txn_date: 0, deposit: 1, description: null };
  // description was suggested and then cleared -> sent as null; untouched empty fields are omitted
  assert.deepEqual(
    buildMapping(sel, init.selection, { account_override: " 기업 123 ", counterparty_override: "  " }, init.formatId),
    {
      format_id: "kr_bank_txn",
      columns: { txn_date: 0, deposit: 1, description: null },
      options: { account_override: "기업 123" },
    },
  );
  assert.deepEqual(duplicateColumns({ a: 1, b: 1, c: 2, d: null }), [1]);
});

test("mapping: a counterparty column stays a column index, the typed name an override", () => {
  const m: MappingOut = {
    ...MAP,
    suggestion: {
      ...MAP.suggestion,
      format_id: "retail_settlement",
      headers: ["메모", "거래처", "금액"],
      matches: [],
      fields: [
        { name: "counterparty", label: "거래처", kind: "text", required: false },
        { name: "amount", label: "정산금액", kind: "amount", required: true },
      ],
    },
  };
  const init = initialSelection(m);
  const req = buildMapping({ counterparty: 1, amount: 2 }, init.selection, {}, init.formatId);
  assert.deepEqual(req.columns, { counterparty: 1, amount: 2 });
  assert.deepEqual(req.options, {});
  assert.equal(Object.keys(req).includes("counterparty"), false);
  const withName = buildMapping({ counterparty: null, amount: 2 }, init.selection, { counterparty_override: "가나유통" });
  assert.deepEqual(withName, { columns: { amount: 2 }, options: { counterparty_override: "가나유통" } });
  // user removed a suggested counterparty column -> explicit null
  const removed = buildMapping({ counterparty: null, amount: 2 }, { counterparty: 1, amount: 2 }, {});
  assert.deepEqual(removed.columns, { counterparty: null, amount: 2 });
});

test("mapping: confirmed mapping takes precedence", () => {
  const m: MappingOut = {
    ...MAP,
    confirmed: {
      mapping: {
        format_id: "kr_bank_txn",
        columns: { txn_date: 0, deposit: 1 },
        options: { counterparty_override: "가나" },
      },
      confirmed_at: "2025-11-01T00:00:00Z",
    },
  };
  const init = initialSelection(m);
  assert.equal(init.selection.txn_date, 0);
  assert.equal(init.selection.deposit, 1);
  assert.equal(init.selection.description, null);
  assert.equal(init.options.counterparty_override, "가나");
  const legacy: MappingOut = { ...MAP, confirmed: { mapping: null, legacy_unreadable: true, confirmed_at: "x" } };
  assert.equal(initialSelection(legacy).selection.description, 2); // falls back to the suggestion
});

// ------------------------------------------------------------------ document application
const OUTCOME: ApplyOutcome = {
  state: "applied_needs_ack",
  message: "v1을(를) 현재 문서로 반영했지만 ...",
  document_id: "doc_1",
  doc_version_id: "docv_1",
  version: 1,
  current_doc_version_id: "docv_1",
  current_version: 1,
  previous_version: null,
  records_added: 3,
  records_updated: 0,
  records_removed: 0,
  ack_required: true,
  acknowledged: false,
  fingerprint: "f".repeat(64),
};

test("apply: states, counts and acknowledgment", () => {
  assert.equal(applyMsgKind(OUTCOME), "pending");
  assert.equal(applyMsgKind({ ...OUTCOME, acknowledged: true }), "ok");
  assert.equal(applyMsgKind({ ...OUTCOME, state: "not_applied_parse" }), "failure");
  assert.equal(applyMsgKind({ ...OUTCOME, state: "not_promoted_older" }), "info");
  assert.equal(canAcknowledge(OUTCOME, true), true);
  assert.equal(canAcknowledge(OUTCOME, false), false);
  assert.equal(canAcknowledge({ ...OUTCOME, acknowledged: true }, true), false);
  assert.equal(
    countsText({ tables_recognized: 1, source_rows: 4, applied_rows: 3, excluded_rows: 1 }),
    "원본 4행 중 3행 반영, 1행 제외",
  );
  assert.equal(countsText({ tables_recognized: 0, source_rows: 0, applied_rows: 0, excluded_rows: 0 }), "인식한 거래 표 없음");
  assert.match(applyTitle({ ...OUTCOME, state: "applied_empty", records_removed: 2 }), /2건 제거/);
  assert.match(applyTitle({ ...OUTCOME, state: "not_promoted_older", current_version: 3 }), /v3/);
  assert.deepEqual(
    mismatchedTotals([
      { table: "t", row: 9, field: "amount", stated: 5000, computed: 1000, matches: false },
      { table: "t", row: 9, field: "fee", stated: 0, computed: 0, matches: true },
    ]).map((t) => t.stated),
    [5000],
  );
  assert.equal(applyStateLabel(null), "반영 전");
  for (const v of Object.values(APPLY_STATE_LABEL)) assert.deepEqual(forbiddenIn(v), [], v);
});

// ------------------------------------------------------------------ decision view
const DETAIL = {
  id: "dec:stl:docv_1:aa#1",
  subject_id: "stl:docv_1:aa#1",
  status: "UNMATCHED",
  review_status: "VERIFIED",
  result_hash: "h1",
  snapshot_hash: "s1",
  required_documents: [],
  unresolved: [],
  missing: [],
  facts: [
    { id: "f1", kind: "settlement_line.settlement_ref", subject_id: "stl:docv_1:aa#1", value: "A-3", extractor: "x", observed_at: "", span: null },
    { id: "f2", kind: "settlement_line.counterparty", subject_id: "stl:docv_1:aa#1", value: "가나유통", extractor: "x", observed_at: "", span: null },
  ],
  computations: [
    {
      name: "recon",
      rule_version: null,
      inputs: { amount: M(3_000_000) },
      outputs: { allocated: M(0), open: M(3_000_000), fee_difference: M(0), candidates: [], reasons: [], status: "UNMATCHED" },
    },
    {
      name: "due",
      rule_version: "kr.large_retail.art8.consignment@2012-01-01",
      inputs: { as_of: "2025-11-01", base_date: "2025-08-31", rollover: null, rounding: "floor", trade_type: "consignment" },
      outputs: {
        base_date: "2025-08-31",
        insufficient: false,
        term_days: 40,
        unresolved: [],
        source_urls: ["https://www.law.go.kr/x"],
        variants: [
          { due_date: "2025-10-10", interest_total: M(28027), label: "rollover_off", max_delay_days: 22, rollover: false, tranches: [] },
        ],
      },
    },
  ],
  variants: [],
  allocations: [],
  assumptions: [],
  rule_versions: [],
  explanation: "",
  checks: [],
  approvals: [],
  history: [],
} as unknown as DecisionDetail;

test("keyNumbers copies engine values (open amount, due variants) without recomputing", () => {
  const k = keyNumbers(DETAIL);
  assert.deepEqual(k.open, M(3_000_000));
  assert.equal(k.due.state, "computed");
  assert.equal(k.due.asOf, "2025-11-01");
  assert.equal(k.due.termDays, 40);
  assert.equal(k.due.variants[0].interest_total?.amount, 28027);
  assert.equal(subjectLabel(DETAIL), "정산 · A-3 · 가나유통");
});

test("keyNumbers: insufficient due computation has no variants", () => {
  const d = {
    ...DETAIL,
    computations: [
      { name: "due", rule_version: null, inputs: { base_date: null }, outputs: { insufficient: true, missing: ["goods_received_date"], notes: ["n"] } },
    ],
  } as unknown as DecisionDetail;
  const k = keyNumbers(d);
  assert.equal(k.due.state, "insufficient");
  assert.deepEqual(k.due.missing, ["goods_received_date"]);
  assert.equal(k.due.variants.length, 0);
  assert.equal(snapshotOf(d, won).due, "계산 보류");
});

test("compare classifies changed / added / removed / review_required", () => {
  const s = (hash: string, review = "VERIFIED"): Snapshot => ({
    status: "MATCHED",
    review_status: review,
    result_hash: hash,
    open: M(0),
    interest: "-",
    due: "-",
    missing: [],
    required_documents: [],
  });
  const before = new Map([["a", s("1")], ["b", s("2")], ["c", s("3")], ["d", s("4", "APPROVED")]]);
  const after = new Map([["a", s("1x")], ["b", s("2")], ["e", s("5")], ["d", s("4", "REVIEW_REQUIRED")]]);
  const rows = compare(["a", "b", "c", "d", "e"], before, after, new Set(["d"]), new Set(["c"]));
  assert.deepEqual(
    rows.map((r) => r.change),
    ["changed", "unchanged", "removed", "review_required", "added"],
  );
});

// ------------------------------------------------------------------ public due
// `jettae rules due --type direct --base 2025-08-09 --paid 2025-10-20 --amount 10000000 --json` 출력 모양
const CANONICAL = {
  $type: "DueResult",
  base_date: { $date: "2025-08-09" },
  due_date: { $date: "2025-10-08" },
  delay_days: 12,
  interest: { $money: [50958, "KRW"] },
  assumptions: ["a"],
  variants: [
    { $type: "DueVariant", label: "rollover_off", rollover: false, due_date: { $date: "2025-10-08" }, delay_days: 12, interest: { $money: [50958, "KRW"] } },
    { $type: "DueVariant", label: "rollover_on", rollover: true, due_date: { $date: "2025-10-10" }, delay_days: 10, interest: { $money: [42465, "KRW"] } },
  ],
  rule_version: "kr.large_retail.art8.direct_purchase@2021-10-21",
  interest_rule_version: "kr.large_retail.delay_interest@2021-13",
  rounding: "floor",
  unresolved: ["rollover"],
  principal: { $money: [10000000, "KRW"] },
  end_date: { $date: "2025-10-20" },
  trade_type: "direct",
  term_days: 60,
  source_urls: ["https://www.law.go.kr/x"],
};

test("normalizePublicDue accepts the canonical CLI form", () => {
  const r = normalizePublicDue(CANONICAL);
  assert.equal(r.result, "due");
  if (r.result !== "due") return;
  assert.equal(r.due_date, "2025-10-08");
  assert.deepEqual(r.variants[1], { label: "rollover_on", rollover: true, due_date: "2025-10-10", delay_days: 10, interest: M(42465) });
  assert.deepEqual(r.unresolved, ["rollover"]);
});

test("normalizePublicDue accepts the plain form and Insufficient", () => {
  const plain = normalizePublicDue({ ...CANONICAL, $type: undefined, due_date: "2025-10-08", base_date: "2025-08-09", interest: M(50958), principal: M(10_000_000), variants: [] });
  assert.equal(plain.result, "due");
  const ins = normalizePublicDue({ $type: "Insufficient", missing: ["sales_close_date"], required_documents: ["월 판매마감 내역"], notes: [] });
  assert.deepEqual(ins, { result: "insufficient", missing: ["sales_close_date"], required_documents: ["월 판매마감 내역"], notes: [] });
  assert.throws(() => normalizePublicDue({ ...CANONICAL, interest: { amount: 1.5, currency: "KRW" } }));
});

test("validateDueInput", () => {
  const ok = { trade_type: "direct", base_date: "2025-08-09", paid_date: "2025-10-20", as_of: null, amount: 1, rollover: null, rounding: "floor" } as const;
  assert.deepEqual(validateDueInput(ok), []);
  assert.equal(validateDueInput({ ...ok, paid_date: "2025-01-01" }).length, 1);
  assert.equal(validateDueInput({ ...ok, amount: -1 }).length, 1);
  assert.deepEqual(validateDueInput({ ...ok, base_date: null }), []);
});

// ------------------------------------------------------------------ wording rule
test("user-facing labels contain no legal-conclusion phrases", () => {
  const maps = [STATUS_LABEL, STATUS_HELP, REVIEW_LABEL, DOC_STATUS_LABEL, DOC_KIND_LABEL, UNRESOLVED_LABEL, MISSING_LABEL];
  for (const m of maps) for (const v of Object.values(m)) assert.deepEqual(forbiddenIn(v), [], v);
  for (const s of ["PARSED", "REGISTERED", "NEEDS_MAPPING", "UNSUPPORTED_SCAN", "CORRUPT", "FAILED"]) {
    assert.deepEqual(forbiddenIn(docStatusMessage(s, "x").text), []);
  }
});

test("no UI source file contains a legal-conclusion phrase", () => {
  const root = join(fileURLToPath(new URL(".", import.meta.url)), "..");
  const files: string[] = [];
  const walk = (dir: string) => {
    for (const n of readdirSync(dir)) {
      const p = join(dir, n);
      if (statSync(p).isDirectory()) walk(p);
      else if (/\.(tsx?|css)$/.test(n) && !n.endsWith(".test.ts") && n !== "fmt.ts") files.push(p);
    }
  };
  walk(root);
  assert.ok(files.length > 10);
  for (const f of files) {
    const hits = FORBIDDEN_PHRASES.filter((p) => readFileSync(f, "utf-8").includes(p));
    assert.deepEqual(hits, [], f);
  }
});

test("evidenceInfo: basis document, corroborating invoice and pending confirmation", () => {
  const linked = {
    computations: [
      {
        name: "evidence",
        rule_version: null,
        inputs: { receivable_id: "stl:1" },
        outputs: {
          basis: "settlement_line",
          basis_id: "stl:1",
          basis_amount: M(11_000_000),
          state: "established",
          counted: true,
          documents: [
            { entity: "settlement_line", id: "stl:1", role: "basis", method: null, amount: M(11_000_000), reason: "" },
            { entity: "invoice", id: "inv:1", role: "corroborating", method: "reference", amount: M(11_000_000), reason: "" },
          ],
          candidates: [],
          amount_conflict: false,
          confirmation_required: null,
          notes: [],
        },
      },
    ],
  } as unknown as DecisionDetail;
  const ev = evidenceInfo(linked)!;
  assert.equal(ev.basis, "settlement_line");
  assert.equal(ev.counted, true);
  assert.deepEqual(
    ev.documents.map((d) => [d.id, d.role, d.method]),
    [
      ["stl:1", "basis", null],
      ["inv:1", "corroborating", "reference"],
    ],
  );
  assert.equal(ev.confirmation, null);

  const pending = {
    computations: [
      {
        name: "evidence",
        rule_version: null,
        inputs: {},
        outputs: {
          basis: "invoice",
          basis_id: "inv:2",
          basis_amount: M(11_000_000),
          state: "needs_confirmation",
          counted: false,
          documents: [{ entity: "invoice", id: "inv:2", role: "basis", method: null, amount: M(11_000_000), reason: "" }],
          candidates: [{ entity: "settlement_line", id: "stl:1", role: "candidate", method: null, amount: M(11_000_000), reason: "" }],
          amount_conflict: false,
          confirmation_required: {
            kind: "evidence_link",
            document_id: "inv:2",
            candidate_settlement_lines: ["stl:1"],
            choices: ["same_sale", "separate_sale"],
          },
          notes: ["n"],
        },
      },
    ],
  } as unknown as DecisionDetail;
  const p = evidenceInfo(pending)!;
  assert.equal(p.counted, false);
  assert.deepEqual(p.confirmation, {
    kind: "evidence_link",
    documentId: "inv:2",
    candidateLines: ["stl:1"],
    choices: ["same_sale", "separate_sale"],
    missing: [],
  });
  assert.equal(evidenceInfo({ computations: [] } as unknown as DecisionDetail), null);
});

test("contractInfo: contractual due date is separate and carries no interest", () => {
  const d = {
    computations: [
      {
        name: "contractual_due",
        rule_version: "contract.payment_term@1",
        inputs: { payment_term_days: 30 },
        outputs: {
          insufficient: false,
          conflict: false,
          term_days: 30,
          base_date: "2025-08-07",
          due_date: "2025-09-06",
          interest: null,
          interest_note: "약정 기한 기준 지연이자 미계산",
          statutory_comparison: [{ label: "rollover_off", statutory_due: "2025-10-06", difference_days: -30 }],
        },
      },
    ],
  } as unknown as DecisionDetail;
  const c = contractInfo(d)!;
  assert.equal(c.state, "computed");
  assert.equal(c.dueDate, "2025-09-06");
  assert.deepEqual(c.comparison, [{ label: "rollover_off", statutoryDue: "2025-10-06", differenceDays: -30 }]);
  const conflict = contractInfo({
    computations: [{ name: "contractual_due", rule_version: null, inputs: {}, outputs: { conflict: true, values: [30, 45] } }],
  } as unknown as DecisionDetail)!;
  assert.equal(conflict.state, "conflict");
  assert.deepEqual(conflict.values, [30, 45]);
});

// ------------------------------------------------------------------ review fixes (2026-10-06)
test("mapping: the suggestion's table is sent with the mapping", () => {
  const m: MappingOut = { ...MAP, suggestion: { ...MAP.suggestion!, table: "9월" } };
  const init = initialSelection(m);
  assert.equal(init.table, "9월");
  const req = buildMapping({ ...init.selection, txn_date: 0, deposit: 1 }, init.selection, {}, init.formatId, init.table);
  assert.equal(req.table, "9월");
  // a mapping confirmed for another table is not used as the starting point
  const other: MappingOut = {
    ...m,
    confirmed: { mapping: { table: "8월", columns: { txn_date: 3 } }, confirmed_at: "x" },
  };
  assert.equal(initialSelection(other).selection.txn_date, null);
  assert.equal(initialSelection(other).selection.description, 2); // the suggestion's match
});

test("apply: unread tables and carried-over records are reported", () => {
  assert.equal(
    countsText({
      tables_recognized: 1,
      source_rows: 1,
      applied_rows: 1,
      excluded_rows: 0,
      tables_unread: 1,
      unread_rows: 2,
    }),
    "원본 1행 중 1행 반영, 0행 제외, 읽지 않은 표 1개(데이터 행 2개)",
  );
  const carried = { ...OUTCOME, records_carried_over: 2, carried_over: ["settlement_line:a", "settlement_line:b"] };
  assert.match(applyTitle(carried), /이전 기록 2건 유지/);
  assert.match(carriedOverText(carried) ?? "", /2건/);
  assert.equal(carriedOverText({ ...carried, acknowledged: true }), null);
  assert.equal(carriedOverText(OUTCOME), null);
});

test("approveErrorView: stale result vs unacknowledged source document", () => {
  const stale = new ApiError(409, "stale_result", "changed", { current_result_hash: "abcdef0123456789" });
  const s = approveErrorView(stale, "0123456789abcdef");
  assert.equal(s.kind, "stale_result");
  assert.equal(s.reload, true);
  assert.match(s.text, /바뀌었습니다/);
  assert.match(s.text, /abcdef0123/);
  assert.deepEqual(s.documents, []);

  const ack = new ApiError(409, "document_ack_required", "ack needed", {
    documents: [{ document_id: "doc_1", doc_version_id: "docv_2", version: 2, state: "applied_needs_ack" }],
  });
  const a = approveErrorView(ack, "0123456789abcdef");
  assert.equal(a.kind, "document_ack_required");
  assert.equal(a.reload, false); // pressing approve again gives the same 409
  assert.doesNotMatch(a.text, /다시 확인 버튼/);
  assert.match(a.text, /반영하지 못한 행/);
  assert.deepEqual(a.documents, [
    { documentId: "doc_1", docVersionId: "docv_2", version: 2, href: "/mapping?dv=docv_2" },
  ]);

  const other = approveErrorView(new ApiError(403, "forbidden", "no"), "h");
  assert.equal(other.kind, "other");
  assert.equal(other.msgKind, "failure");
});

test("evidenceInfo / pendingText: duplicate line and unknown invoice direction", () => {
  const dir = {
    computations: [
      {
        name: "evidence",
        rule_version: null,
        inputs: {},
        outputs: {
          basis: "invoice",
          basis_id: "inv:9",
          basis_amount: M(1100),
          state: "needs_confirmation",
          counted: false,
          documents: [],
          candidates: [],
          amount_conflict: false,
          confirmation_required: {
            kind: "invoice_direction",
            document_id: "inv:9",
            candidate_settlement_lines: [],
            choices: [],
            missing: ["counterparty", "direction"],
          },
          notes: [],
        },
      },
    ],
  } as unknown as DecisionDetail;
  const ev = evidenceInfo(dir)!;
  assert.equal(ev.confirmation?.kind, "invoice_direction");
  assert.deepEqual(ev.confirmation?.choices, []);
  assert.deepEqual(ev.confirmation?.missing, ["counterparty", "direction"]);
  assert.match(pendingText("invoice_direction"), /매출·매입/);
  assert.match(pendingText("duplicate_line"), /다른 문서/);
  for (const k of ["evidence_link", "duplicate_line", "invoice_direction"]) {
    assert.deepEqual(forbiddenIn(pendingText(k)), [], k);
  }
});
