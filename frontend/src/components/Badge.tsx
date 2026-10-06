import {
  DOC_STATUS_LABEL,
  JOB_STATUS_LABEL,
  REVIEW_LABEL,
  STATUS_HELP,
  STATUS_LABEL,
  label,
} from "@/lib/fmt";

const STATUS_TONE: Record<string, string> = {
  MATCHED: "b-ok",
  PARTIAL: "b-warn",
  UNMATCHED: "b-bad",
  AMBIGUOUS: "b-warn",
  CONFLICT: "b-bad",
  INSUFFICIENT_EVIDENCE: "b-neutral",
};

const REVIEW_TONE: Record<string, string> = {
  DRAFT: "b-neutral",
  VERIFIED: "b-info",
  APPROVED: "b-ok",
  REVIEW_REQUIRED: "b-warn",
  SUPERSEDED: "b-neutral",
};

const DOC_TONE: Record<string, string> = {
  REGISTERED: "b-info",
  PARSED: "b-ok",
  NEEDS_MAPPING: "b-warn",
  UNSUPPORTED_SCAN: "b-bad",
  CORRUPT: "b-bad",
  FAILED: "b-bad",
};

const JOB_TONE: Record<string, string> = {
  queued: "b-info",
  running: "b-info",
  succeeded: "b-ok",
  failed: "b-bad",
  cancelled: "b-neutral",
};

/** 거래별 대사 상태 배지. 코드(MATCHED 등)와 한국어 설명을 함께 보여 준다. */
export function StatusBadge({ status }: { status: string }) {
  return (
    <span className={`badge ${STATUS_TONE[status] ?? "b-neutral"}`} title={STATUS_HELP[status] ?? status}>
      {label(STATUS_LABEL, status)} <span className="mono">{status}</span>
    </span>
  );
}

export function ReviewBadge({ status }: { status: string }) {
  return (
    <span
      className={`badge ${REVIEW_TONE[status] ?? "b-neutral"}`}
      title={status === "VERIFIED" ? "기계적 검사(숫자·인용·문구)를 통과했다는 뜻이며 법적 판단이 아닙니다." : status}
    >
      {label(REVIEW_LABEL, status)}
    </span>
  );
}

export function DocBadge({ status }: { status: string }) {
  return <span className={`badge ${DOC_TONE[status] ?? "b-neutral"}`}>{label(DOC_STATUS_LABEL, status)}</span>;
}

export function JobBadge({ status }: { status: string }) {
  return <span className={`badge ${JOB_TONE[status] ?? "b-neutral"}`}>{label(JOB_STATUS_LABEL, status)}</span>;
}
