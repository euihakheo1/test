/**
 * 표시용 순수 함수(브라우저·Node 둘 다에서 동작; node --test 로 검사).
 *
 * 표현 원칙(SPEC §1): 차이 / 필요 서류 / 확인이 필요한 조건 / 관련 근거로만 말한다.
 * 법적 결론(위법 여부, 받을 권리 등)을 나타내는 말은 쓰지 않는다 → FORBIDDEN_PHRASES 검사.
 */

import type { Locator, Money } from "./api.ts";

// 백엔드 jettae.verify.checks.FORBIDDEN_PHRASES 와 같은 목록 + 화면 전용 추가 항목.
export const FORBIDDEN_PHRASES = [
  "위법",
  "불법",
  "받을 수 있",
  "받을 수있",
  "청구할 수 있",
  "승소",
  "회수 가능",
  "법 위반",
] as const;

export function forbiddenIn(text: string): string[] {
  return FORBIDDEN_PHRASES.filter((p) => text.includes(p));
}

// ------------------------------------------------------------------ 상태 이름
// 백엔드 jettae.app.explain.STATUS_LABEL 과 같은 말을 쓴다.
export const STATUS_LABEL: Record<string, string> = {
  MATCHED: "입금액 일치",
  PARTIAL: "일부 입금",
  UNMATCHED: "입금 미확인",
  AMBIGUOUS: "입금 대상 특정 불가",
  CONFLICT: "조건 충돌",
  INSUFFICIENT_EVIDENCE: "근거 부족(계산 보류)",
};

export const STATUS_HELP: Record<string, string> = {
  MATCHED: "정산 금액과 연결된 입금 합계가 같습니다.",
  PARTIAL: "연결된 입금이 정산 금액보다 적습니다. 차액을 확인하세요.",
  UNMATCHED: "이 거래에 연결된 입금을 찾지 못했습니다.",
  AMBIGUOUS: "같은 금액의 후보가 여럿이라 어느 입금인지 정하지 않았습니다(판단 보류).",
  CONFLICT: "문서·약정 사이에 서로 다른 조건이 있습니다. 어느 쪽이 맞는지 확인이 필요합니다.",
  INSUFFICIENT_EVIDENCE: "기준일 등 계산 근거가 없어 계산하지 않았습니다(판단 보류). 필요 서류를 확인하세요.",
};

export const REVIEW_LABEL: Record<string, string> = {
  DRAFT: "초안",
  VERIFIED: "기계 검사 통과",
  APPROVED: "확인됨",
  REVIEW_REQUIRED: "재확인 필요",
  SUPERSEDED: "대체됨",
};

export const DOC_STATUS_LABEL: Record<string, string> = {
  REGISTERED: "읽기 대기",
  PARSED: "읽기 완료",
  NEEDS_MAPPING: "열 매핑 확인 필요",
  UNSUPPORTED_SCAN: "스캔 PDF(처리 불가)",
  CORRUPT: "파일 손상",
  FAILED: "읽기 실패",
};

export const DOC_KIND_LABEL: Record<string, string> = {
  settlement: "정산서",
  tax_invoice: "세금계산서",
  bank: "입금 내역(은행)",
  agreement: "약정서·계약서",
  delivery: "입고·하차 기록",
  sales_close: "판매마감 내역",
  other: "기타(자동 판별)",
};

export const JOB_TYPE_LABEL: Record<string, string> = {
  ingest_document: "문서 읽기",
  run_analysis: "분석",
  apply_change: "변경 적용",
  investigate_decision: "Agent 조사",
};

export const JOB_STATUS_LABEL: Record<string, string> = {
  queued: "대기 중",
  running: "실행 중",
  succeeded: "완료",
  failed: "처리 실패",
  cancelled: "취소됨",
};

export const VARIANT_LABEL: Record<string, string> = {
  rollover_off: "말일 그대로",
  rollover_on: "다음 영업일 이월",
};

export const UNRESOLVED_LABEL: Record<string, string> = {
  rollover: "기한 말일이 휴일일 때 다음 영업일 이월 여부",
  allocation: "입금이 어느 거래에 대한 것인지",
  agreement_conflict: "약정 조건 간 차이",
  trade_type_conflict: "거래 형태(문서와 약정의 차이)",
  interest_rule_version: "지연기간에 적용할 이율 고시 버전",
  evidence_link: "같은 거래의 정산 행과 세금계산서인지",
  duplicate_line: "다른 문서에 같은 참조번호로 있는 정산 행과 같은 행인지(정산서 중복 업로드)",
  invoice_direction: "세금계산서의 매출·매입 구분과 거래처",
  evidence_amount_conflict: "연결된 문서 간 금액 차이",
  contract_term_base: "약정 지급기한의 기산점",
  contract_term_conflict: "약정 간 지급기한 일수 차이",
};

export const MISSING_LABEL: Record<string, string> = {
  goods_received_date: "상품수령일",
  sales_close_date: "월 판매마감일",
  trade_type: "거래 형태(직매입·특약매입 등)",
  counterparty: "거래처",
  direction: "매출·매입 구분",
  base_date: "기준일",
  amount: "금액",
};

export const TRADE_TYPE_LABEL: Record<string, string> = {
  direct: "직매입",
  consignment: "특약매입·위수탁·임대을",
  subcontract: "하도급",
};

export const CONFIDENCE_LABEL: Record<string, string> = {
  confirmed: "확인됨",
  high: "높음",
  medium: "중간",
  low: "낮음",
  none: "없음",
};

export const label = (map: Record<string, string>, key: string | null | undefined): string =>
  key == null ? "-" : (map[key] ?? key);

// ------------------------------------------------------------------ 숫자·날짜
const WON = new Intl.NumberFormat("ko-KR");

/** 정수 원 단위 표시. 정수가 아니면(계약 위반) 그대로 문자열로 보여 준다. */
export function won(m: Money | number | null | undefined): string {
  if (m === null || m === undefined) return "-";
  const amount = typeof m === "number" ? m : m.amount;
  const cur = typeof m === "number" ? "KRW" : m.currency;
  if (!Number.isInteger(amount)) return `${String(amount)} ${cur}`;
  return cur === "KRW" ? `${WON.format(amount)}원` : `${WON.format(amount)} ${cur}`;
}

export function isMoney(v: unknown): v is Money {
  return (
    typeof v === "object" &&
    v !== null &&
    typeof (v as Money).amount === "number" &&
    typeof (v as Money).currency === "string"
  );
}

/** 원 단위 입력 문자열("1,234,000" 등) → 정수. 형식이 틀리면 null. */
export function parseWon(s: string): number | null {
  const t = s.replace(/[,\s원]/g, "");
  if (!/^\d{1,15}$/.test(t)) return null;
  const n = Number(t);
  return Number.isSafeInteger(n) ? n : null;
}

const KST = new Intl.DateTimeFormat("ko-KR", {
  timeZone: "Asia/Seoul",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
});

/** 시스템 시각(UTC aware ISO) → 한국 시간 표시. */
export function kst(iso: string | null | undefined): string {
  if (!iso) return "-";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return `${KST.format(d)} (KST)`;
}

/** 업무 날짜(YYYY-MM-DD)는 시간대 변환 없이 그대로. */
export function bizDate(s: unknown): string {
  return typeof s === "string" && /^\d{4}-\d{2}-\d{2}$/.test(s) ? s : s == null ? "-" : String(s);
}

export function isIsoDate(s: string): boolean {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(s)) return false;
  const [y, m, d] = s.split("-").map(Number);
  const dt = new Date(Date.UTC(y, m - 1, d));
  return dt.getUTCFullYear() === y && dt.getUTCMonth() === m - 1 && dt.getUTCDate() === d;
}

export function shortHash(h: string | null | undefined, n = 10): string {
  return h ? h.slice(0, n) : "-";
}

export function bytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

/** 표시용 값 문자열화(금액은 원 단위). */
export function showValue(v: unknown): string {
  if (v === null || v === undefined) return "-";
  if (isMoney(v)) return won(v);
  if (typeof v === "string") return v;
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  return JSON.stringify(v);
}

// ------------------------------------------------------------------ 원문 위치
const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);
const str = (v: unknown): string | null => (typeof v === "string" && v ? v : null);

/**
 * SourceSpan.locator → 사람이 읽는 위치 설명.
 * CSV: 표·행·열(문자 범위), XLSX: 시트·셀(병합 범위), PDF: 쪽·표·문자 범위.
 */
export function describeLocator(loc: Locator | null | undefined): string {
  if (!loc) return "위치 정보 없음";
  const parts: string[] = [];
  const sheet = str(loc.sheet);
  const page = num(loc.page);
  const cell = str(loc.cell);
  const row = num(loc.row);
  const colLetter = str(loc.col_letter);
  const col = num(loc.col);
  const header = str(loc.header);
  const cs = num(loc.char_start);
  const ce = num(loc.char_end);
  if (page !== null) parts.push(`${page}쪽`);
  if (sheet) parts.push(`시트 '${sheet}'`);
  if (!sheet && page === null) {
    const t = str(loc.table_name);
    if (t && t !== "csv") parts.push(`표 '${t}'`);
  }
  if (page !== null && loc.table !== undefined && loc.table !== null) parts.push(`표 ${String(loc.table)}`);
  if (cell) {
    parts.push(`셀 ${cell}`);
  } else {
    if (row !== null) parts.push(`${row}행`);
    if (colLetter) parts.push(`${colLetter}열`);
    else if (col !== null) parts.push(`${col}번째 열`);
  }
  if (str(loc.merged)) parts.push(`병합 ${String(loc.merged)}`);
  if (header) parts.push(`(${header})`);
  if (cs !== null && ce !== null) parts.push(`문자 ${cs}–${ce}`);
  if (loc.ocr) parts.push("[OCR]");
  return parts.length ? parts.join(" · ") : "위치 정보 없음";
}

/**
 * 원문 텍스트에서 [start, end) 범위를 앞뒤 문맥과 함께 잘라낸다.
 * 범위의 글자가 발췌(excerpt)와 다르면 matches=false (원문 위치를 확인할 수 없음).
 */
export function sliceContext(
  text: string,
  start: number,
  end: number,
  excerpt: string,
  pad = 60,
): { before: string; hit: string; after: string; matches: boolean } | null {
  if (!Number.isInteger(start) || !Number.isInteger(end) || start < 0 || end < start || end > text.length) {
    return null;
  }
  const hit = text.slice(start, end);
  const lineStart = Math.max(text.lastIndexOf("\n", start - 1) + 1, start - pad);
  let lineEnd = text.indexOf("\n", end);
  if (lineEnd === -1) lineEnd = text.length;
  lineEnd = Math.min(lineEnd, end + pad);
  return {
    before: text.slice(lineStart, start),
    hit,
    after: text.slice(end, lineEnd),
    matches: hit === excerpt,
  };
}

/** 원문 바이트 → 텍스트. utf-8(BOM 제거)을 먼저, 실패하면 cp949(euc-kr). */
export function decodeText(buf: ArrayBuffer): { text: string; encoding: string } | null {
  const tryDecode = (enc: string): string | null => {
    try {
      return new TextDecoder(enc, { fatal: true }).decode(buf);
    } catch {
      return null;
    }
  };
  let t = tryDecode("utf-8");
  if (t !== null) return { text: t.replace(/^﻿/, ""), encoding: "utf-8" };
  t = tryDecode("euc-kr");
  if (t !== null) return { text: t, encoding: "cp949" };
  return null;
}

// ------------------------------------------------------------------ 메시지 구분
/** 처리 실패 / 자료 없음 / 판단 보류 / 안내 를 구분한다. */
export type MsgKind = "failure" | "empty" | "pending" | "info" | "ok";

export const MSG_TITLE: Record<MsgKind, string> = {
  failure: "처리 실패",
  empty: "자료 없음",
  pending: "판단 보류",
  info: "안내",
  ok: "완료",
};

/** 문서 상태 → 메시지 종류와 설명. */
export function docStatusMessage(
  status: string,
  reason?: string | null,
): { kind: MsgKind; text: string } {
  switch (status) {
    case "PARSED":
      return { kind: "ok", text: "문서를 읽었습니다." };
    case "REGISTERED":
      return { kind: "info", text: "문서를 저장했고 읽기 작업을 기다리는 중입니다." };
    case "NEEDS_MAPPING":
      return {
        kind: "pending",
        text: "열 이름을 알아보지 못했습니다. 열 매핑을 확인하면 다시 읽습니다.",
      };
    case "UNSUPPORTED_SCAN":
      return {
        kind: "failure",
        text: "스캔(이미지) PDF입니다. OCR이 설정되지 않아 읽지 못했습니다. 거래가 없다는 뜻이 아닙니다. 원본 엑셀·CSV·텍스트 PDF를 올려 주세요.",
      };
    case "CORRUPT":
      return {
        kind: "failure",
        text: `파일이 손상되었거나 형식이 맞지 않습니다${reason ? ` (${reason})` : ""}. 거래가 없다는 뜻이 아닙니다.`,
      };
    case "FAILED":
      return {
        kind: "failure",
        text: `문서를 읽지 못했습니다${reason ? ` (${reason})` : ""}. 거래가 없다는 뜻이 아닙니다.`,
      };
    default:
      return { kind: "info", text: status };
  }
}

/** 판단 보류에 해당하는 대사 상태. */
export function isPendingStatus(s: string): boolean {
  return s === "INSUFFICIENT_EVIDENCE" || s === "AMBIGUOUS" || s === "CONFLICT";
}

export function errorText(e: unknown): string {
  if (e && typeof e === "object" && "code" in e && "message" in e) {
    const x = e as { status?: number; code: string; message: string; requestId?: string | null };
    const known: Record<string, string> = {
      network_error:
        "서버에 연결하지 못했습니다. API 서버 실행 상태와 화면 빌드 때 설정한 JETTAE_API_ORIGIN 을 확인하세요.",
      unauthorized: "로그인이 필요합니다.",
      csrf_failed: "보안 확인(CSRF)에 실패했습니다. 화면을 새로 고친 뒤 다시 시도하세요.",
      forbidden: "이 작업을 할 권한이 없습니다.",
      not_found: "찾을 수 없습니다.",
      payload_too_large: "파일이 너무 큽니다.",
      unsupported_media_type: "지원하지 않는 파일 형식입니다.",
      stale_result: "결과가 바뀌었습니다. 최신 결과를 다시 확인하세요.",
      report_not_valid: "현재 확인(승인)되지 않은 결과가 있습니다.",
      ingest_unavailable: "문서 읽기 모듈을 사용할 수 없습니다.",
      duplicate_content: "같은 내용의 파일이 다른 문서로 이미 저장되어 있습니다.",
      document_ack_required:
        "이 결과의 근거 문서에 반영하지 못한 행이나 합계 차이가 있습니다. 문서의 제외 행을 먼저 확인하세요.",
      stale_fingerprint: "문서를 다시 읽어 내용이 바뀌었습니다. 새로 불러와 다시 확인하세요.",
      not_current_version: "현재 장부에 반영된 버전만 확인할 수 있습니다.",
      nothing_to_acknowledge: "이 버전은 모든 행을 반영해 확인할 것이 없습니다.",
      invalid_mapping: "열 매핑 형식이 맞지 않습니다.",
      direction_unknown:
        "이 세금계산서는 매출·매입 구분 또는 거래처를 알 수 없어 같은 거래 여부를 확인할 수 없습니다. 열 매핑에서 우리 회사 사업자등록번호나 매출/매입 구분을 지정해 다시 읽으세요.",
    };
    const base = known[x.code] ?? x.message;
    const rid = x.requestId ? ` [요청 ID ${x.requestId}]` : "";
    return `${base}${x.status ? ` (HTTP ${x.status}, ${x.code})` : ""}${rid}`;
  }
  return e instanceof Error ? e.message : String(e);
}

// ------------------------------------------------------------------ 업로드 안내
export const UPLOAD_EXTENSIONS = [".csv", ".txt", ".xlsx", ".pdf"] as const;

export function checkUploadFile(
  name: string,
  size: number,
  maxBytes: number,
): string | null {
  const lower = name.toLowerCase();
  const dot = lower.lastIndexOf(".");
  const ext = dot >= 0 ? lower.slice(dot) : "";
  if (!(UPLOAD_EXTENSIONS as readonly string[]).includes(ext)) {
    return `지원하지 않는 형식(${ext || "확장자 없음"})입니다. ${UPLOAD_EXTENSIONS.join(", ")} 파일만 올릴 수 있습니다.`;
  }
  if (size === 0) return "빈 파일입니다.";
  if (size > maxBytes) return `파일이 너무 큽니다(${bytes(size)} > ${bytes(maxBytes)}).`;
  return null;
}
