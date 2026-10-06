/**
 * 문서 반영 결과(서버 jettae.app.doc_apply)를 화면 문구로 바꾸는 순수 함수.
 *
 * - "양식을 읽음"(document_status = PARSED)과 "모든 거래를 반영함"(state = applied)은 다르다.
 * - applied_needs_ack: 현재 문서로 반영했지만 제외된 행·읽지 못한 칸·합계 차이가 있다.
 *   사용자가 확인하기 전까지 이 문서를 근거로 한 결과는 승인할 수 없다.
 * - applied_empty: 거래 행이 0개인 정정본이 확인되어 이전 기록을 지웠다(지운 건수 표시).
 * - not_promoted_older: 더 새 버전이 이미 반영되어 있어 이 버전은 장부에 반영하지 않았다.
 * - not_applied_*: 읽지 못했거나 표가 없거나 모든 행이 제외되어 기존 기록을 그대로 두었다.
 */

import type { ApplyOutcome, ApplyState, RowCounts, RowIssue, TotalCheck } from "./api.ts";
import { errorText, shortHash, type MsgKind } from "./fmt.ts";

export const APPLY_STATE_LABEL: Record<ApplyState, string> = {
  applied: "모두 반영",
  applied_needs_ack: "일부 행 미반영 · 확인 필요",
  applied_empty: "빈 정정본 반영",
  unchanged: "변경 없음",
  not_promoted_older: "이전 버전(미반영)",
  not_applied_parse: "읽지 못함(기존 유지)",
  not_applied_no_table: "표 없음(기존 유지)",
  not_applied_all_excluded: "모든 행 제외(기존 유지)",
  not_applied_empty_unverified: "빈 표·합계 불일치(기존 유지)",
};

export const ISSUE_KIND_LABEL: Record<RowIssue["kind"], string> = {
  excluded: "행 제외",
  value: "칸 비움",
};

export function applyStateLabel(s: string | null | undefined): string {
  if (!s) return "반영 전";
  return (APPLY_STATE_LABEL as Record<string, string>)[s] ?? s;
}

export function applyStateTone(s: string | null | undefined): string {
  switch (s) {
    case "applied":
    case "unchanged":
      return "b-ok";
    case "applied_needs_ack":
    case "applied_empty":
      return "b-warn";
    case "not_promoted_older":
      return "b-neutral";
    case undefined:
    case null:
      return "b-info";
    default:
      return "b-bad";
  }
}

/** 메시지 상자 종류: 반영 안 됨은 실패가 아니라 "판단 보류/안내"로 보여 준다. */
export function applyMsgKind(o: ApplyOutcome): MsgKind {
  switch (o.state) {
    case "applied":
    case "unchanged":
      return "ok";
    case "applied_needs_ack":
      return o.acknowledged ? "ok" : "pending";
    case "applied_empty":
      return "pending";
    case "not_promoted_older":
      return "info";
    case "not_applied_parse":
      return "failure";
    default:
      return "pending";
  }
}

export function applyTitle(o: ApplyOutcome): string {
  if (o.state === "applied_needs_ack") {
    if (o.acknowledged) return "제외 행 확인됨";
    const carried = o.records_carried_over ?? 0;
    return carried > 0 ? `일부 행을 반영하지 않음 · 이전 기록 ${carried}건 유지` : "일부 행을 반영하지 않음";
  }
  if (o.state === "applied_empty") return `빈 정정본 · 이전 기록 ${o.records_removed}건 제거`;
  if (o.state === "not_promoted_older") return `현재 문서는 v${o.current_version ?? "-"}`;
  return APPLY_STATE_LABEL[o.state] ?? o.state;
}

/** "원본 n행 중 m행 반영, k행 제외" (+ 양식을 알아보지 못해 읽지 않은 표) */
export function countsText(c: RowCounts | null | undefined): string {
  if (!c) return "-";
  const unread = c.tables_unread ?? 0;
  const unreadText = unread > 0 ? `, 읽지 않은 표 ${unread}개(데이터 행 ${c.unread_rows ?? 0}개)` : "";
  if (c.tables_recognized === 0) return `인식한 거래 표 없음${unreadText}`;
  return `원본 ${c.source_rows}행 중 ${c.applied_rows}행 반영, ${c.excluded_rows}행 제외${unreadText}`;
}

/** 이 버전에서 읽지 못해 지우지 않고 남겨 둔 이전 버전 기록 안내(없으면 null). */
export function carriedOverText(o: ApplyOutcome | null | undefined): string | null {
  const n = o?.records_carried_over ?? 0;
  if (!o || n <= 0) return null;
  if (o.acknowledged) return null;
  return (
    `이전 버전의 기록 ${n}건은 이 버전에서 대응하는 행을 읽지 못해 지우지 않고 남겨 두었습니다. ` +
    "제외 행을 확인하면 이 기록은 제거됩니다. 행이 실제로 남아 있어야 한다면 파일을 고쳐 다시 올리세요."
  );
}

// ------------------------------------------------------------------ 승인(확인) 오류
/** 승인 요청이 거부된 이유를 화면 문구로 바꾼 결과. */
export interface ApproveErrorView {
  kind: "stale_result" | "document_ack_required" | "other";
  msgKind: MsgKind;
  title?: string;
  text: string;
  /** true: 최신 결과를 다시 불러온다(결과가 바뀐 경우만). */
  reload: boolean;
  /** 확인(acknowledge)해야 하는 문서 버전과 그 문서 화면 링크. */
  documents: { documentId: string; docVersionId: string; version: number | null; href: string }[];
}

interface ErrorLike {
  status?: number;
  code?: string;
  details?: Record<string, unknown> | null;
}

function asErrorLike(e: unknown): ErrorLike | null {
  return e && typeof e === "object" && "code" in e ? (e as ErrorLike) : null;
}

/**
 * POST /decisions/{id}/approve 의 오류 해석. HTTP 409 는 두 가지다.
 * - stale_result: 보고 있던 결과가 바뀜 → 다시 불러와 확인하게 한다.
 * - document_ack_required: 근거 문서에 반영하지 못한 행·읽지 않은 표·합계 차이가 있고 아직 확인되지 않음 →
 *   다시 눌러도 같은 오류이므로 다시 누르라고 하지 않고, 확인할 문서(문서 화면)를 알려 준다.
 */
export function approveErrorView(e: unknown, viewedHash: string): ApproveErrorView {
  const x = asErrorLike(e);
  if (x && x.status === 409 && x.code === "document_ack_required") {
    const raw = Array.isArray(x.details?.documents) ? (x.details?.documents as unknown[]) : [];
    const documents = raw
      .filter((d): d is Record<string, unknown> => typeof d === "object" && d !== null)
      .map((d) => {
        const docVersionId = typeof d.doc_version_id === "string" ? d.doc_version_id : "";
        return {
          documentId: typeof d.document_id === "string" ? d.document_id : "",
          docVersionId,
          version: typeof d.version === "number" ? d.version : null,
          href: `/mapping?dv=${encodeURIComponent(docVersionId)}`,
        };
      })
      .filter((d) => d.docVersionId !== "");
    return {
      kind: "document_ack_required",
      msgKind: "pending",
      title: "근거 문서 확인 필요",
      text: `${errorText(e)} 아래 문서 화면에서 제외 행을 확인한 뒤 승인할 수 있습니다.`,
      reload: false,
      documents,
    };
  }
  if (x && x.status === 409 && (x.code === "stale_result" || x.code === undefined)) {
    const cur = typeof x.details?.current_result_hash === "string" ? x.details.current_result_hash : "";
    return {
      kind: "stale_result",
      msgKind: "pending",
      title: "결과 바뀜",
      text: `보고 있던 결과(${shortHash(viewedHash)})가 그 사이 바뀌었습니다(현재 ${shortHash(cur)}). 최신 결과를 다시 불러왔습니다. 내용을 확인한 뒤 다시 확인 버튼을 누르세요.`,
      reload: true,
      documents: [],
    };
  }
  return { kind: "other", msgKind: "failure", text: errorText(e), reload: false, documents: [] };
}

export function mismatchedTotals(totals: TotalCheck[] | null | undefined): TotalCheck[] {
  return (totals ?? []).filter((t) => !t.matches);
}

/** 확인 버튼을 보여 줄지: 현재 버전이고, 확인이 필요하고, 아직 확인하지 않았을 때. */
export function canAcknowledge(o: ApplyOutcome | null | undefined, isCurrent: boolean): boolean {
  return Boolean(o && isCurrent && o.ack_required && !o.acknowledged && o.state === "applied_needs_ack");
}
