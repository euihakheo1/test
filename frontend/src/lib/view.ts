/**
 * 결정 상세(DecisionDetail) → 화면용 요약. 수치는 엔진이 낸 값을 그대로 옮기며,
 * 여기서 새로 계산하지 않는다(차액 = 엔진의 recon.outputs.open).
 */

import type { Computation, DecisionDetail, DueVariant, FactOut, Money } from "./api.ts";
import { isMoney } from "./fmt.ts";

export interface KeyNumbers {
  amount: Money | null; // 정산(청구) 금액
  allocated: Money | null; // 연결된 입금 배분액
  open: Money | null; // 미결(차액)
  feeDifference: Money | null; // 허용 오차로 처리한 차액
  reconReasons: string[];
  candidates: unknown[];
  due: DueInfo;
}

export interface DueInfo {
  state: "computed" | "insufficient" | "none";
  ruleVersion: string | null;
  interestRuleVersion: string | null;
  baseDate: string | null;
  asOf: string | null;
  termDays: number | null;
  tradeType: string | null;
  rounding: string | null;
  rolloverSetting: boolean | null;
  variants: DueVariant[];
  unresolved: string[];
  sourceUrls: string[];
  missing: string[];
  notes: string[];
}

export function computation(d: Pick<DecisionDetail, "computations">, name: string): Computation | null {
  return d.computations.find((c) => c.name === name) ?? null;
}

const money = (v: unknown): Money | null => (isMoney(v) ? v : null);
const strs = (v: unknown): string[] => (Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : []);
const s = (v: unknown): string | null => (typeof v === "string" ? v : null);

function asVariants(v: unknown): DueVariant[] {
  if (!Array.isArray(v)) return [];
  return v.filter(
    (x): x is DueVariant =>
      typeof x === "object" && x !== null && typeof (x as DueVariant).due_date === "string",
  );
}

export function keyNumbers(d: Pick<DecisionDetail, "computations" | "variants">): KeyNumbers {
  const recon = computation(d, "recon");
  const due = computation(d, "due");
  const ro = recon?.outputs ?? {};
  const ri = recon?.inputs ?? {};
  const dueOut = due?.outputs ?? {};
  const dueIn = due?.inputs ?? {};
  const insufficient = due ? Boolean(dueOut.insufficient) : false;
  const variants = d.variants.length ? d.variants : insufficient ? [] : asVariants(dueOut.variants);
  return {
    amount: money(ri.amount),
    allocated: money(ro.allocated),
    open: money(ro.open),
    feeDifference: money(ro.fee_difference),
    reconReasons: strs(ro.reasons),
    candidates: Array.isArray(ro.candidates) ? ro.candidates : [],
    due: {
      state: !due ? "none" : insufficient ? "insufficient" : "computed",
      ruleVersion: due?.rule_version ?? null,
      interestRuleVersion: s(dueOut.interest_rule_version),
      baseDate: s(dueOut.base_date) ?? s(dueIn.base_date),
      asOf: s(dueIn.as_of),
      termDays: typeof dueOut.term_days === "number" ? dueOut.term_days : null,
      tradeType: s(dueIn.trade_type),
      rounding: s(dueIn.rounding),
      rolloverSetting: typeof dueIn.rollover === "boolean" ? dueIn.rollover : null,
      variants,
      unresolved: strs(dueOut.unresolved),
      sourceUrls: strs(dueOut.source_urls),
      missing: strs(dueOut.missing),
      notes: strs(dueOut.notes),
    },
  };
}

const REF_FIELDS = ["settlement_ref", "reference", "approval_no", "invoice_no", "item", "item_name"];
const PARTY_FIELDS = ["counterparty", "party_name", "buyer_name"];
const ENTITY_LABEL: Record<string, string> = {
  stl: "정산",
  inv: "세금계산서",
  bank: "입금",
  agr: "약정",
};

/** 거래를 사람이 알아볼 이름: "정산 A-1 · 가나유통" (사실에서 찾고, 없으면 ID). */
export function subjectLabel(d: Pick<DecisionDetail, "subject_id" | "facts">): string {
  const own = d.facts.filter((f) => f.subject_id === d.subject_id);
  const field = (names: string[]): string | null => {
    for (const n of names) {
      const f = own.find((x) => x.kind.endsWith(`.${n}`) && typeof x.value === "string" && x.value);
      if (f) return f.value as string;
    }
    return null;
  };
  const prefix = d.subject_id.split(":", 1)[0];
  const parts = [ENTITY_LABEL[prefix] ?? prefix, field(REF_FIELDS), field(PARTY_FIELDS)].filter(Boolean);
  return parts.length > 1 ? parts.join(" · ") : d.subject_id;
}

/** 사실을 문서 버전별로 묶는다(원문 미리보기용). 위치 없는 사실은 "" 키. */
export function factsByDoc(facts: FactOut[]): Map<string, FactOut[]> {
  const m = new Map<string, FactOut[]>();
  for (const f of facts) {
    const k = f.span?.doc_version_id ?? "";
    const list = m.get(k) ?? [];
    list.push(f);
    m.set(k, list);
  }
  return m;
}

/** "kr.large_retail.art8.direct_purchase@2021-10-21" → {id, version}. */
export function splitRuleVersion(key: string): { id: string; version: string } {
  const i = key.lastIndexOf("@");
  return i < 0 ? { id: key, version: "" } : { id: key.slice(0, i), version: key.slice(i + 1) };
}

/**
 * 규칙 ID → 제목·출처(표시용). `jettae rules list` 의 등록 내용을 옮긴 것(2026-10-06 기준).
 * 계산에 실제로 쓴 출처는 결정의 due 계산 결과(source_urls)가 우선이다.
 */
export const RULE_INFO: Record<string, { title: string; url: string }> = {
  "kr.large_retail.art8.direct_purchase": {
    title: "대규모유통업법 제8조 제2항(직매입: 상품수령일부터 60일 이내)",
    url: "https://www.law.go.kr/법령/대규모유통업에서의거래공정화에관한법률/제8조",
  },
  "kr.large_retail.art8.consignment": {
    title: "대규모유통업법 제8조 제1항(특약매입 등: 판매마감일부터 40일 이내)",
    url: "https://www.law.go.kr/법령/대규모유통업에서의거래공정화에관한법률/제8조",
  },
  "kr.large_retail.delay_interest": {
    title: "상품판매대금 등 지연지급 시의 지연이율 고시(제2021-13호): 연 15.5%",
    url: "https://www.law.go.kr/DRF/lawService.do?OC=test&target=admrul&ID=2100000205723&type=HTML",
  },
  "kr.subcontract.art13": {
    title: "하도급법 제13조 제1항(목적물 등의 수령일부터 60일 이내)",
    url: "https://www.law.go.kr/법령/하도급거래공정화에관한법률/제13조",
  },
  "kr.subcontract.delay_interest": {
    title: "선급금 등 지연지급 시의 지연이율 고시(제2018-21호): 연 15.5%",
    url: "https://www.law.go.kr/DRF/lawService.do?OC=test&target=admrul&ID=2100000171352&type=HTML",
  },
};

// ------------------------------------------------------------------ 이전/현재 비교
export interface Snapshot {
  status: string;
  review_status: string;
  result_hash: string;
  open: Money | null;
  interest: string; // 변형별 지연이자 요약
  due: string; // 변형별 지급기한 요약
  missing: string[];
  required_documents: string[];
}

function variantSummary(vs: DueVariant[], pick: (v: DueVariant) => string): string {
  if (!vs.length) return "-";
  return vs.map((v) => (vs.length > 1 ? `${v.label === "rollover_on" ? "이월" : "말일"} ${pick(v)}` : pick(v))).join(" / ");
}

export function snapshotOf(
  d: Pick<DecisionDetail, "status" | "review_status" | "result_hash" | "computations" | "variants" | "missing" | "required_documents">,
  wonFmt: (m: Money | null) => string,
): Snapshot {
  const k = keyNumbers(d);
  return {
    status: d.status,
    review_status: d.review_status,
    result_hash: d.result_hash,
    open: k.open,
    interest:
      k.due.state === "insufficient"
        ? "계산 보류"
        : variantSummary(k.due.variants, (v) => wonFmt(v.interest_total)),
    due:
      k.due.state === "insufficient"
        ? "계산 보류"
        : variantSummary(k.due.variants, (v) => v.due_date),
    missing: d.missing,
    required_documents: d.required_documents,
  };
}

export interface CompareRow {
  id: string;
  change: "changed" | "added" | "removed" | "review_required" | "unchanged";
  before: Snapshot | null;
  after: Snapshot | null;
}

/** 이전 스냅숏(Map)과 현재 스냅숏(Map)을 비교한다. ids 순서를 유지. */
export function compare(
  ids: string[],
  before: Map<string, Snapshot>,
  after: Map<string, Snapshot>,
  reviewRequired: Set<string>,
  removed: Set<string>,
): CompareRow[] {
  return ids.map((id) => {
    const b = before.get(id) ?? null;
    const a = after.get(id) ?? null;
    let change: CompareRow["change"];
    if (removed.has(id) || (b && !a)) change = "removed";
    else if (!b && a) change = "added";
    else if (b && a && b.result_hash !== a.result_hash) change = "changed";
    else if (reviewRequired.has(id) || a?.review_status === "REVIEW_REQUIRED") change = "review_required";
    else change = "unchanged";
    return { id, change, before: b, after: a };
  });
}

// ------------------------------------------------------------------ 문서 증빙(문서 행 ≠ 미수 채권)
/**
 * 엔진의 evidence 계산을 화면용으로 옮긴다. 정산 행과 같은 거래의 세금계산서는 하나의 채권이며,
 * 금액은 '금액 기준' 문서 하나에서만 온다(보강 증빙은 금액을 더하지 않음). counted=false 인 결과는
 * 같은 거래인지 확인을 기다리는 문서로, 미수 합계와 입금 배분에서 빠진다(확인 대기).
 */
export interface EvidenceDoc {
  entity: string;
  id: string;
  role: string; // basis | corroborating | candidate
  method: string | null; // reference | user_confirmation
  amount: Money | null;
  reason: string;
}

export interface EvidenceInfo {
  basis: string;
  basisId: string;
  basisAmount: Money | null;
  counted: boolean;
  state: string;
  amountConflict: boolean;
  documents: EvidenceDoc[];
  candidates: EvidenceDoc[];
  /** 확인이 필요할 때: 후보 정산 행 ID 와 선택지. */
  /**
   * 확인 대기 이유(kind): evidence_link = 세금계산서가 정산 행과 같은 거래인지,
   * duplicate_line = 먼저 올린 다른 문서의 같은 참조번호 정산 행과 같은 행인지(정산서 중복),
   * invoice_direction = 매출·매입 구분 또는 거래처를 몰라 받을 돈인지 모름(선택지 없음: 열 매핑에서 지정).
   */
  confirmation: {
    kind: string;
    documentId: string;
    candidateLines: string[];
    choices: string[];
    missing: string[];
  } | null;
  notes: string[];
}

export const DOC_ENTITY_LABEL: Record<string, string> = { invoice: "세금계산서", settlement_line: "정산 행" };
export const EVIDENCE_ROLE_LABEL: Record<string, string> = {
  basis: "금액 기준",
  corroborating: "보강 증빙",
  candidate: "연결 후보",
};
/** 확인 대기 안내 문구(kind 별). */
export function pendingText(kind: string | null | undefined): string {
  switch (kind) {
    case "duplicate_line":
      return "먼저 올린 다른 문서에 같은 거래처·같은 참조번호의 정산 행이 있습니다. 같은 정산서를 다시 올린 것인지 확인되지 않아, 확인 전까지 미수 합계와 입금 배분에서 제외합니다.";
    case "invoice_direction":
      return "이 세금계산서의 매출·매입 구분 또는 거래처(공급받는자)를 알 수 없어 받을 돈인지 확인되지 않았습니다. 미수 합계와 입금 배분에서 제외합니다. 문서의 열 매핑에서 우리 회사 사업자등록번호나 매출/매입 구분을 지정해 다시 읽으세요.";
    default:
      return "이 세금계산서가 같은 거래처의 정산 행과 같은 거래인지 확인되지 않았습니다. 확인 전까지 미수 합계와 입금 배분에서 제외합니다. 금액·날짜가 같다는 이유만으로는 연결하지 않습니다.";
  }
}

export const LINK_METHOD_LABEL: Record<string, string> = {
  reference: "참조번호 일치",
  user_confirmation: "사용자 확인",
};

function asDocs(v: unknown): EvidenceDoc[] {
  if (!Array.isArray(v)) return [];
  return v
    .filter((x): x is Record<string, unknown> => typeof x === "object" && x !== null)
    .map((x) => ({
      entity: s(x.entity) ?? "",
      id: s(x.id) ?? "",
      role: s(x.role) ?? "",
      method: s(x.method),
      amount: money(x.amount),
      reason: s(x.reason) ?? "",
    }));
}

export function evidenceInfo(d: Pick<DecisionDetail, "computations">): EvidenceInfo | null {
  const c = computation(d, "evidence");
  if (!c) return null;
  const o = c.outputs;
  const conf = o.confirmation_required;
  let confirmation: EvidenceInfo["confirmation"] = null;
  if (typeof conf === "object" && conf !== null) {
    const r = conf as Record<string, unknown>;
    confirmation = {
      kind: s(r.kind) ?? "evidence_link",
      missing: strs(r.missing),
      documentId: s(r.document_id) ?? "",
      candidateLines: strs(r.candidate_settlement_lines),
      choices: strs(r.choices),
    };
  }
  return {
    basis: s(o.basis) ?? "",
    basisId: s(o.basis_id) ?? "",
    basisAmount: money(o.basis_amount),
    counted: o.counted !== false,
    state: s(o.state) ?? "",
    amountConflict: o.amount_conflict === true,
    documents: asDocs(o.documents),
    candidates: asDocs(o.candidates),
    confirmation,
    notes: strs(o.notes),
  };
}

/** 약정 기한(약정서의 지급기한 일수). 법정 지연이율은 적용하지 않으므로 이자 값이 없다. */
export interface ContractInfo {
  state: "computed" | "insufficient" | "conflict";
  termDays: number | null;
  values: number[];
  dueDate: string | null;
  baseDate: string | null;
  comparison: { label: string; statutoryDue: string; differenceDays: number }[];
  interestNote: string;
}

export function contractInfo(d: Pick<DecisionDetail, "computations">): ContractInfo | null {
  const c = computation(d, "contractual_due");
  if (!c) return null;
  const o = c.outputs;
  const comparison = Array.isArray(o.statutory_comparison)
    ? o.statutory_comparison
        .filter((x): x is Record<string, unknown> => typeof x === "object" && x !== null)
        .map((x) => ({
          label: s(x.label) ?? "",
          statutoryDue: s(x.statutory_due) ?? "",
          differenceDays: typeof x.difference_days === "number" ? x.difference_days : 0,
        }))
    : [];
  return {
    state: o.conflict === true ? "conflict" : o.insufficient === true ? "insufficient" : "computed",
    termDays: typeof o.term_days === "number" ? o.term_days : null,
    values: Array.isArray(o.values) ? o.values.filter((x): x is number => typeof x === "number") : [],
    dueDate: s(o.due_date),
    baseDate: s(o.base_date),
    comparison,
    interestNote: s(o.interest_note) ?? "약정 기한에는 법정 지연이율을 적용하지 않음",
  };
}
