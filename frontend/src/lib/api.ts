/**
 * 제때받기 HTTP API 클라이언트 (한 파일).
 *
 * 타입과 경로는 백엔드 FastAPI 앱(src/jettae/api, /api/v1/openapi.json)에 맞췄다.
 * 경로가 바뀌면 이 파일만 고치면 된다.
 *
 * - 항상 같은 출처의 /api/v1/* 를 부른다. next.config.ts 의 rewrites 가 JETTAE_API_ORIGIN 으로 넘긴다.
 *   인증 쿠키(jt_access Path=/api, jt_csrf Path=/)가 화면 출처에 심겨야 CSRF 토큰을 스크립트가 읽을 수
 *   있으므로, 다른 출처의 API 를 직접 부르는 방식은 지원하지 않는다.
 * - 인증: 서버가 심는 HttpOnly 쿠키. 이 모듈은 토큰을 읽지도 저장하지도 않는다(Authorization 헤더 없음).
 *   POST/PUT/PATCH/DELETE 에는 jt_csrf 쿠키 값을 X-CSRF-Token 으로 보낸다(없으면 서버가 403 csrf_failed).
 *   401 이면 POST /auth/refresh 를 한 번만 부르고(동시 요청은 한 번의 refresh 를 공유), 성공하면
 *   원래 요청을 한 번 다시 보낸다. refresh 가 실패하면 화면 로그인 상태를 지운다.
 * - 테넌트는 서버가 인증 정보로 정한다. 클라이언트는 tenant_id 를 보내지 않는다.
 * - 금액은 정수(원) {amount, currency}. 부동소수점 금액을 만들지 않는다.
 * - 결정 ID 에는 ':' 와 '#' 가 들어 있으므로 경로에 넣을 때 반드시 encodeURIComponent.
 */

import { normalizePublicDue } from "./due.ts";
import {
  clearSession,
  getSessionState,
  setSession,
  setSessionError,
  type Session,
} from "./session.ts";

export const API_PREFIX = "/api/v1";
export const CSRF_COOKIE = "jt_csrf";
export const CSRF_HEADER = "X-CSRF-Token";

// ------------------------------------------------------------------ 공통 타입
export interface Money {
  amount: number; // 정수(원)
  currency: string;
}

export type ReconcileStatus =
  | "MATCHED"
  | "PARTIAL"
  | "UNMATCHED"
  | "AMBIGUOUS"
  | "CONFLICT"
  | "INSUFFICIENT_EVIDENCE";

export type ReviewStatus = "DRAFT" | "VERIFIED" | "APPROVED" | "REVIEW_REQUIRED" | "SUPERSEDED";

export type DocumentStatus =
  | "REGISTERED"
  | "PARSED"
  | "NEEDS_MAPPING"
  | "UNSUPPORTED_SCAN"
  | "CORRUPT"
  | "FAILED";

export type DocKind =
  | "settlement"
  | "tax_invoice"
  | "bank"
  | "agreement"
  | "delivery"
  | "sales_close"
  | "other";

export type JobType = "ingest_document" | "run_analysis" | "apply_change" | "investigate_decision";
export type JobStatus = "queued" | "running" | "succeeded" | "failed" | "cancelled";

export interface ErrorBody {
  code: string;
  message: string;
  details: Record<string, unknown> | null;
  request_id: string | null;
}

// ------------------------------------------------------------------ auth
/**
 * GET /auth/me 응답. 계약은 {user, tenant} 이며, 이전 평면 형식(user_id, email, tenant_id, ...)도
 * 읽는다(normalizeMe). 서버 배포 순서가 화면과 어긋나도 로그인 상태를 잘못 읽지 않게 하기 위해서다.
 */
export interface MeResponse {
  user?: { id?: string; user_id?: string; email?: string; role?: string; auth_method?: string } | null;
  tenant?: { id?: string; tenant_id?: string; name?: string | null; role?: string } | null;
  user_id?: string;
  email?: string;
  tenant_id?: string;
  tenant_name?: string | null;
  role?: string;
  auth_method?: string;
}

// ------------------------------------------------------------------ investigations (Agent)
export type InvestigationStrategy = "single" | "roles";
export type InvestigationMode = "offline" | "replay" | "live";
export type InvestigationStatus = "queued" | "running" | "succeeded" | "failed" | "refused";

export interface InvestigationCitation {
  doc_version_id: string;
  locator: Locator;
  excerpt: string;
}

export interface InvestigationFinding {
  kind: string;
  message: string;
  required_documents: string[];
  citations: InvestigationCitation[];
}

export interface InvestigationUsage {
  steps: number;
  tool_calls: number;
  input_tokens: number;
  output_tokens: number;
  /** 정수(원). 오프라인·재생은 0. */
  cost_krw: number;
}

export interface Investigation {
  id: string;
  strategy: InvestigationStrategy;
  mode: InvestigationMode;
  status: InvestigationStatus;
  findings: InvestigationFinding[];
  usage: InvestigationUsage | null;
  error: string | { code?: string; message?: string } | null;
  created_at: string;
}

export interface InvestigationCapabilities {
  modes_enabled: InvestigationMode[];
  live_enabled: boolean;
}

export interface InvestigationAccepted {
  investigation_id: string;
  job_id: string;
}

// ------------------------------------------------------------------ documents
export interface DocumentVersion {
  id: string;
  document_id: string;
  version: number;
  filename: string;
  media_type: string;
  kind: DocKind;
  size: number;
  content_hash: string;
  status: DocumentStatus;
  supersedes: string | null;
  created_at: string;
  status_detail: StatusDetail | null;
  /** 이 버전을 반영한 결과(applied 등). null = 아직 읽기 전. status 는 "양식을 읽었는지"만 뜻한다. */
  apply_state: ApplyState | null;
  issue_count: number | null;
  /** 이 버전의 행이 현재 장부에 들어 있는지. */
  is_current: boolean;
  /** 현재 장부에 반영된 버전 번호(없으면 null). */
  current_version: number | null;
}

export interface StatusDetail {
  reason?: string | null;
  notes?: string[];
  suggestion?: MappingSuggestion | null;
  counts?: RowCounts | null;
  issues?: RowIssue[];
  issues_total?: number;
  totals?: TotalCheck[];
  application?: ApplyOutcome;
  [k: string]: unknown;
}

// ------------------------------------------------------------------ 문서 반영 (app/doc_apply.py)
export type ApplyState =
  | "applied"
  | "applied_needs_ack"
  | "applied_empty"
  | "unchanged"
  | "not_promoted_older"
  | "not_applied_parse"
  | "not_applied_no_table"
  | "not_applied_all_excluded"
  | "not_applied_empty_unverified";

/**
 * 사용한 표의 행 수. source_rows = applied_rows + excluded_rows (머리글·빈 행·합계 행 제외).
 * tables_unread / unread_rows: 데이터 행이 있지만 양식을 알아보지 못해 읽지 않은 표(예: 머리글이 다른 시트).
 * 이 행들은 반영도 제외도 아닌 "읽지 않음"이며, 있으면 반영 상태는 applied_needs_ack 이다.
 */
export interface RowCounts {
  tables_recognized: number;
  source_rows: number;
  applied_rows: number;
  excluded_rows: number;
  tables_unread?: number;
  unread_rows?: number;
}

/** excluded: 행을 거래로 반영하지 않음. value: 행은 반영했지만 이 칸을 읽지 못해 비워 둠. */
export interface RowIssue {
  table: string;
  row: number;
  field: string | null;
  message: string;
  kind: "excluded" | "value";
}

/** 표의 합계 행과 데이터 행 합계 비교. */
export interface TotalCheck {
  table: string;
  row: number;
  field: string;
  stated: number;
  computed: number;
  matches: boolean;
}

export interface ApplyOutcome {
  state: ApplyState;
  message: string;
  document_id: string;
  doc_version_id: string;
  version: number;
  current_doc_version_id: string | null;
  current_version: number | null;
  previous_version: number | null;
  records_added: number;
  records_updated: number;
  records_removed: number;
  /** 이 버전에서 대응하는 행을 읽지 못해 지우지 않고 남겨 둔 이전 버전 기록(확인하면 제거). */
  records_carried_over?: number;
  carried_over?: string[];
  ack_required: boolean;
  acknowledged: boolean;
  fingerprint: string;
}

export interface DocumentHead {
  document_id: string;
  doc_version_id: string;
  version: number;
  state: ApplyState | "legacy";
  fingerprint: string;
  needs_ack: boolean;
  acknowledged: boolean;
  applied_at: string;
  ack_by: string | null;
  ack_at: string | null;
  /** 확인(acknowledge) 응답에만: 확인으로 제거한 이전 버전 기록 수와 목록. */
  records_removed?: number | null;
  removed?: string[] | null;
}

export interface UploadResponse {
  document: DocumentVersion;
  duplicate: boolean;
  job_id: string | null;
}

export interface ChangeUploadResponse extends UploadResponse {
  status: string; // "queued" | "unchanged"
  status_url: string | null;
}

export interface DocumentPage {
  items: { document_id: string; latest: DocumentVersion }[];
  next_cursor: string | null;
}

export interface VersionList {
  document_id: string;
  items: DocumentVersion[];
  current: DocumentHead | null;
}

/** SourceSpan.locator: CSV row/col/col_letter/line/char_*, XLSX sheet/cell/merged,
 *  PDF page/table/bbox/char_*; 항상 table_name·header 포함(수집 모듈 계약). */
export type Locator = Record<string, unknown>;

export interface SpanItem {
  fact_id: string;
  kind: string;
  subject_id: string | null;
  value: unknown;
  locator: Locator;
  excerpt: string;
}

export interface SpanPage {
  items: SpanItem[];
  next_cursor: string | null;
}

export interface MappingMatch {
  field: string;
  column: number | null;
  header: string | null;
  confidence: string; // confirmed | high | medium | low | none
  reason: string;
}

export interface MappingField {
  name: string;
  label: string;
  kind: string; // text | id | amount | date | time
  required: boolean;
}

export interface MappingSuggestion {
  format_id?: string | null;
  needs_confirmation?: boolean;
  overall?: string;
  matches?: MappingMatch[];
  unmapped_required?: string[];
  unmapped_columns?: number[];
  sources?: string[];
  table?: string;
  headers?: string[];
  fields?: MappingField[];
  [k: string]: unknown;
}

export interface MappingOut {
  doc_version_id: string;
  document_status: DocumentStatus;
  suggestion: MappingSuggestion | null;
  confirmed: {
    /** null: 예전 형식으로 저장되어 지금 형식으로 읽을 수 없는 매핑(다시 확정 필요). */
    mapping: MappingRequest | null;
    legacy_unreadable?: boolean;
    confirmed_at: string;
    [k: string]: unknown;
  } | null;
}

/** 문서 단위 값(열 참조가 아님). 서버 계약: jettae.app.contracts.MappingOptions. */
export interface MappingOptions {
  counterparty_override?: string | null;
  account_override?: string | null;
  self_brn?: string | null;
  direction?: "sales" | "purchase" | null;
  accept_suggested?: boolean;
}

/**
 * 열 매핑 요청(서버 계약 jettae.app.contracts.MappingRequest).
 * columns: 항목 → 열 번호(0부터). null = 사용자가 그 항목의 열을 지움.
 * options: 모든 행에 적용할 값. 거래처 열은 columns.counterparty, 거래처 이름은 options.counterparty_override.
 */
export interface MappingRequest {
  format_id?: string | null;
  /** 이 열 번호들이 가리키는 표(제안의 table). 표가 여러 개인 파일에서는 반드시 보낸다. */
  table?: string | null;
  columns: Record<string, number | null>;
  options?: MappingOptions;
}

export interface MappingConfirmed {
  mapping_id: string;
  doc_version_id: string;
  job_id: string | null;
}

// ------------------------------------------------------------------ jobs
export interface ImpactPlan {
  affected: string[];
  reasons: Record<string, string[]>;
  fallback_full: boolean;
  removed: string[];
  notes: string[];
  summary: string;
}

export interface ChangeOutcome {
  plan: ImpactPlan;
  recomputed_groups: string[];
  changed: string[];
  review_required: string[];
  removed: string[];
  snapshot_hash: string;
}

export interface IngestJobResult {
  doc_version_id: string;
  document_id: string;
  version: number;
  /** 양식을 읽었는지(PARSED). 모든 거래를 반영했는지는 application 이 말한다. */
  document_status: DocumentStatus;
  reason: string | null;
  facts: number;
  records: number;
  counts: RowCounts | null;
  issues: RowIssue[];
  issues_total: number;
  totals: TotalCheck[];
  notes: string[];
  application: ApplyOutcome;
  impact?: ChangeOutcome | null;
}

export interface AnalysisJobResult {
  snapshot_hash: string;
  decisions: number;
  by_status: Record<string, number>;
  by_review_status: Record<string, number>;
  unattributed_payments: string[][];
}

export interface Job {
  id: string;
  type: JobType;
  status: JobStatus;
  attempts: number;
  max_attempts: number;
  payload: Record<string, unknown>;
  result: unknown;
  error: { code?: string; type?: string; message?: string } | null;
  cancel_requested: boolean;
  created_by: string | null;
  created_at: string;
  updated_at: string;
  run_after: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface JobAccepted {
  job_id: string;
  status: string;
  status_url: string;
}

export interface JobPage {
  items: Job[];
  next_cursor: string | null;
}

export interface AnalysisParams {
  as_of?: string | null;
  rollover?: boolean | null;
  rounding?: "floor" | "half_up" | null;
}

// ------------------------------------------------------------------ decisions
export interface DecisionSummary {
  id: string;
  subject_id: string;
  status: ReconcileStatus;
  review_status: ReviewStatus;
  result_hash: string;
  snapshot_hash: string;
  required_documents: string[];
  unresolved: string[];
  missing: string[];
  /** 금액 기준 문서(settlement_line | invoice). 증빙 계산이 없는 결과는 null. */
  basis?: string | null;
  basis_id?: string | null;
  /** false = 같은 거래인지 확인 대기 중인 문서: 미수 합계에 더하지 않는다(확인 대기). */
  counted?: boolean | null;
}

export interface EvidenceLinkOut {
  id: string;
  invoice_id: string;
  relation: "same_sale" | "separate_sale";
  settlement_line_id: string | null;
  confirmed_by: string;
  note: string;
}

export interface LinkOutcome {
  link: EvidenceLinkOut | null;
  changed: string[];
  review_required: string[];
  removed: string[];
  snapshot_hash: string;
}

export interface EvidenceLinkRequest {
  relation: "same_sale" | "separate_sale";
  settlement_line_id?: string | null;
  invoice_id?: string | null;
  note?: string;
}

export interface DecisionPage {
  items: DecisionSummary[];
  next_cursor: string | null;
}

export interface SourceSpan {
  doc_version_id: string;
  locator: Locator;
  excerpt: string;
}

export interface FactOut {
  id: string;
  kind: string;
  subject_id: string | null;
  value: unknown;
  extractor: string;
  observed_at: string;
  span: SourceSpan | null;
}

export interface Computation {
  name: string; // "recon" | "due" | ...
  rule_version: string | null;
  inputs: Record<string, unknown>;
  outputs: Record<string, unknown>;
}

export interface Tranche {
  amount: Money;
  delay_days: number;
  end_date: string;
  interest: Money | null;
  source: string;
}

export interface DueVariant {
  label: string; // rollover_off | rollover_on
  rollover: boolean;
  due_date: string;
  max_delay_days: number | null; // null: 입금 배분 미확정(AMBIGUOUS)이라 계산하지 않음
  interest_total: Money | null;
  tranches: Tranche[];
}

export interface Approval {
  id: string;
  decision_id: string;
  result_hash: string;
  snapshot_hash: string;
  approved_by: string;
  approved_at: string;
  current: boolean;
}

export interface DecisionDetail extends DecisionSummary {
  facts: FactOut[];
  computations: Computation[];
  variants: DueVariant[];
  allocations: Record<string, unknown>[];
  assumptions: string[];
  rule_versions: string[];
  explanation: string;
  checks: { name: string; passed: boolean; details: string[] }[];
  approvals: Approval[];
  history: { recorded_at: string; result_hash: string; status: string; superseded: boolean }[];
}

export interface DecisionFilter {
  status?: string[];
  review_status?: string[];
  subject_id?: string;
  cursor?: string | null;
  limit?: number;
}

// ------------------------------------------------------------------ public due (무료 검산)
/**
 * POST /api/v1/public/due  (인증 없음, IP별 분당 요청 제한). 서버가 404/405 를 주면 화면은 '서버 미제공'으로
 * 표시하고 브라우저에서 계산을 흉내 내지 않는다(휴일 달력·규칙 버전은 서버에만 있음).
 * 요청·응답 계약은 CLI `jettae rules due` 와 같다(값은 to_plain 형식: 날짜 ISO, 금액 {amount,currency}).
 */
export interface PublicDueRequest {
  trade_type: "direct" | "consignment" | "subcontract";
  base_date: string | null; // 상품수령일·판매마감일 (YYYY-MM-DD)
  paid_date: string | null; // 지급일
  as_of: string | null; // 미지급이면 계산 기준일
  amount: number; // 원금(원, 정수)
  rollover: boolean | null; // null = 두 계산 모두
  rounding: "floor" | "half_up";
}

export interface PublicDueVariant {
  label: string;
  rollover: boolean;
  due_date: string;
  delay_days: number;
  interest: Money | null;
}

export interface PublicDueComputed {
  result: "due";
  base_date: string;
  due_date: string;
  delay_days: number;
  interest: Money | null;
  assumptions: string[];
  variants: PublicDueVariant[];
  rule_version: string;
  interest_rule_version: string | null;
  rounding: string;
  unresolved: string[];
  principal: Money;
  end_date: string | null;
  trade_type: string;
  term_days: number;
  source_urls: string[];
}

export interface PublicDueInsufficient {
  result: "insufficient";
  missing: string[];
  required_documents: string[];
  notes: string[];
}

export type PublicDueResponse = PublicDueComputed | PublicDueInsufficient;

// ------------------------------------------------------------------ 오류
export type FailureKind = "network" | "http";

export class ApiError extends Error {
  status: number;
  code: string;
  details: Record<string, unknown> | null;
  requestId: string | null;
  kind: FailureKind;

  constructor(
    status: number,
    code: string,
    message: string,
    details: Record<string, unknown> | null = null,
    requestId: string | null = null,
    kind: FailureKind = "http",
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
    this.requestId = requestId;
    this.kind = kind;
  }
}

function newKey(): string {
  const c = globalThis.crypto;
  if (c && typeof c.randomUUID === "function") return c.randomUUID();
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

export function url(path: string): string {
  return `${API_PREFIX}${path}`;
}

const UNSAFE = new Set(["POST", "PUT", "PATCH", "DELETE"]);

/** document.cookie 에서 jt_csrf 값을 읽는다(서버 렌더링·테스트에서 document 가 없으면 null). */
export function readCsrfToken(): string | null {
  const doc = (globalThis as { document?: { cookie?: string } }).document;
  const all = doc?.cookie ?? "";
  for (const part of all.split(";")) {
    const i = part.indexOf("=");
    if (i < 0) continue;
    if (part.slice(0, i).trim() === CSRF_COOKIE) {
      const v = part.slice(i + 1).trim();
      try {
        return v ? decodeURIComponent(v) : null;
      } catch {
        return v || null;
      }
    }
  }
  return null;
}

/** 경로 조각(결정 ID 등) 인코딩. */
export const seg = (s: string): string => encodeURIComponent(s);

async function toError(res: Response): Promise<ApiError> {
  let body: { error?: ErrorBody } | null = null;
  try {
    body = (await res.json()) as { error?: ErrorBody };
  } catch {
    body = null;
  }
  const e = body?.error;
  return new ApiError(
    res.status,
    e?.code ?? `http_${res.status}`,
    e?.message ?? res.statusText ?? "request failed",
    e?.details ?? null,
    e?.request_id ?? res.headers.get("x-request-id"),
  );
}

interface ReqOpts {
  method?: string;
  json?: unknown;
  form?: FormData;
  /** false: 로그인·가입·공개 엔드포인트. 401 이어도 refresh 하지 않는다. 기본 true. */
  auth?: boolean;
  idempotent?: boolean; // POST 생성 요청에 Idempotency-Key
  signal?: AbortSignal;
}

function send(path: string, init: { method: string; headers?: Record<string, string>; body?: BodyInit; signal?: AbortSignal }): Promise<Response> {
  const headers: Record<string, string> = { ...(init.headers ?? {}) };
  if (UNSAFE.has(init.method)) {
    const csrf = readCsrfToken();
    if (csrf) headers[CSRF_HEADER] = csrf;
  }
  // credentials: "include" — 같은 출처라 "same-origin" 과 결과가 같지만, 쿠키를 꼭 보내야 하는
  // 요청이라는 뜻을 분명히 하고 프록시 앞단 주소가 달라져도 쿠키가 빠지지 않게 한다.
  return globalThis.fetch(url(path), {
    method: init.method,
    headers,
    body: init.body,
    signal: init.signal,
    credentials: "include",
    cache: "no-store",
  });
}

/**
 * refresh 결과. "ended" 는 서버가 세션이 끝났다고 답한 경우(401/403)뿐이다. 429·5xx·네트워크 오류는
 * "unavailable": 서버가 지금 답하지 못한 것이지 로그인이 끝난 것이 아니다. 이때 로그인 상태를 지우면
 * 다른 사람의 로그인 시도로 걸린 속도 제한이나 잠깐의 서버 오류만으로 사용자가 화면에서 로그아웃된다.
 */
type RefreshOutcome =
  | { kind: "refreshed" }
  | { kind: "ended" }
  | { kind: "unavailable"; status: number; requestId: string | null; network: boolean };

let refreshing: Promise<RefreshOutcome> | null = null;

/**
 * refresh 쿠키로 새 access 쿠키를 받는다. 동시에 401 을 받은 요청들이 refresh 를 여러 번 보내면
 * 서버가 회전된 refresh 토큰의 재사용으로 볼 수 있으므로 한 화면(JS 컨텍스트) 안에서는 한 번의 요청을
 * 공유한다. 여러 탭이 거의 동시에 보내는 경우는 서버의 짧은 재사용 유예(JETTAE_REFRESH_REUSE_GRACE_S)가
 * 받는다. 401/403 이면 로그인 상태를 지우고, 그 밖의 실패는 상태를 "error" 로 둔다.
 */
function tryRefresh(): Promise<RefreshOutcome> {
  if (!refreshing) {
    refreshing = (async (): Promise<RefreshOutcome> => {
      try {
        const res = await send("/auth/refresh", { method: "POST" });
        if (res.ok) return { kind: "refreshed" };
        if (res.status === 401 || res.status === 403) {
          clearSession();
          return { kind: "ended" };
        }
        setSessionError();
        return { kind: "unavailable", status: res.status, requestId: res.headers.get("x-request-id"), network: false };
      } catch {
        setSessionError();
        return { kind: "unavailable", status: 0, requestId: null, network: true };
      } finally {
        refreshing = null;
      }
    })();
  }
  return refreshing;
}

const NO_REFRESH = new Set(["/auth/refresh", "/auth/logout", "/auth/login", "/auth/signup"]);

async function raw(path: string, o: ReqOpts = {}, retried = false): Promise<Response> {
  const headers: Record<string, string> = {};
  const auth = o.auth ?? true;
  let body: BodyInit | undefined;
  if (o.json !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(o.json);
  } else if (o.form) {
    body = o.form;
  }
  if (o.idempotent) headers["Idempotency-Key"] = idemKey(path, o);
  let res: Response;
  try {
    res = await send(path, {
      method: o.method ?? (body ? "POST" : "GET"),
      headers,
      body,
      signal: o.signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") throw err;
    throw new ApiError(0, "network_error", "서버에 연결하지 못했습니다", null, null, "network");
  }
  if (res.status === 401 && auth && !retried && !NO_REFRESH.has(path)) {
    // 재시도는 한 번뿐이다. refresh 뒤에도 401 이면 그대로 오류로 돌려준다(무한 반복 방지).
    const outcome = await tryRefresh();
    if (outcome.kind === "refreshed") return raw(path, o, true);
    if (outcome.kind === "unavailable") {
      // 로그인 여부를 모르는 상태: 401 로 돌려주면 호출한 쪽이 로그아웃으로 처리한다
      throw new ApiError(
        outcome.status,
        "session_unavailable",
        "로그인 상태를 지금 확인하지 못했습니다. 잠시 후 다시 시도하세요",
        null,
        outcome.requestId,
        outcome.network ? "network" : "http",
      );
    }
  }
  if (res.status === 401 && auth) clearSession();
  if (!res.ok) throw await toError(res);
  return res;
}

// 같은 요청 객체로 재시도할 때 같은 키를 쓰도록 요청별로 한 번만 만든다.
const keys = new WeakMap<ReqOpts, string>();
function idemKey(_path: string, o: ReqOpts): string {
  let k = keys.get(o);
  if (!k) {
    k = newKey();
    keys.set(o, k);
  }
  return k;
}

async function json<T>(path: string, o: ReqOpts = {}): Promise<T> {
  const res = await raw(path, o);
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

function qs(params: Record<string, string | number | string[] | null | undefined>): string {
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === null || v === undefined || v === "") continue;
    if (Array.isArray(v)) v.forEach((x) => u.append(k, x));
    else u.append(k, String(v));
  }
  const s = u.toString();
  return s ? `?${s}` : "";
}

/**
 * /auth/me 응답 → 화면 상태. 응답 본문에 토큰이 섞여 와도 여기서 필요한 표시 항목만 고르므로
 * 토큰이 메모리 상태로 들어오지 않는다.
 */
export function normalizeMe(raw: unknown): Session {
  const r = (raw ?? {}) as MeResponse;
  const u = r.user ?? {};
  const t = r.tenant ?? {};
  const str = (...xs: unknown[]): string => {
    for (const x of xs) if (typeof x === "string" && x) return x;
    return "";
  };
  const tenantName = t.name ?? r.tenant_name ?? null;
  return {
    userId: str(u.id, u.user_id, r.user_id),
    email: str(u.email, r.email),
    role: str(u.role, t.role, r.role),
    tenantId: str(t.id, t.tenant_id, r.tenant_id),
    tenantName: typeof tenantName === "string" && tenantName ? tenantName : null,
    authMethod: str(u.auth_method, r.auth_method) || null,
  };
}

let loadingSession: Promise<Session | null> | null = null;

/**
 * 쿠키 로그인 상태를 서버에 묻는다(GET /auth/me, 필요하면 refresh 1회). 여러 화면 요소가 동시에
 * 불러도 요청은 한 번이다. 401 → anonymous, 네트워크 오류 → error(로그인 여부 모름).
 */
export function loadSession(force = false): Promise<Session | null> {
  if (!force && getSessionState().status === "authenticated") {
    return Promise.resolve(getSessionState().session);
  }
  if (!loadingSession) {
    loadingSession = (async () => {
      try {
        const s = normalizeMe(await json<unknown>("/auth/me"));
        setSession(s);
        return s;
      } catch (e) {
        if (e instanceof ApiError && (e.status === 401 || e.status === 403)) clearSession();
        else setSessionError();
        return null;
      } finally {
        loadingSession = null;
      }
    })();
  }
  return loadingSession;
}

// ------------------------------------------------------------------ 엔드포인트
export const api = {
  // auth — 응답 본문의 토큰은 쓰지 않는다(쿠키 세션은 본문에 토큰이 없다는 계약). 로그인 뒤
  // /auth/me 로 표시 정보를 다시 읽어 서버가 정한 회사·역할만 보여 준다.
  async signup(email: string, password: string, tenantName: string): Promise<Session | null> {
    await raw("/auth/signup", { json: { email, password, tenant_name: tenantName }, auth: false });
    return loadSession(true);
  },
  async login(email: string, password: string, tenantId?: string): Promise<Session | null> {
    await raw("/auth/login", { json: { email, password, tenant_id: tenantId || null }, auth: false });
    return loadSession(true);
  },
  /** 서버가 refresh 토큰 묶음을 폐기하고 세 쿠키를 지운다. 서버 오류여도 화면 상태는 지운다. */
  async logout(): Promise<void> {
    try {
      await raw("/auth/logout", { method: "POST", auth: false });
    } catch {
      // 쿠키는 HttpOnly 라 스크립트로 지울 수 없다. 서버 폐기가 실패하면 access 쿠키는 만료까지 남는다.
    } finally {
      clearSession();
    }
  },
  me: async (): Promise<Session> => normalizeMe(await json<unknown>("/auth/me")),

  // documents
  uploadDocument(file: File, kind: DocKind, documentId?: string): Promise<UploadResponse> {
    const f = new FormData();
    f.append("file", file, file.name);
    f.append("kind", kind);
    if (documentId) f.append("document_id", documentId);
    return json<UploadResponse>("/documents", { form: f, idempotent: true });
  },
  listDocuments: (cursor?: string | null, limit = 50) =>
    json<DocumentPage>(`/documents${qs({ cursor, limit })}`),
  versions: (documentId: string) =>
    json<VersionList>(`/documents/${seg(documentId)}/versions`),
  version: (dv: string) => json<DocumentVersion>(`/document-versions/${seg(dv)}`),
  spans: (dv: string, cursor?: string | null, limit = 200) =>
    json<SpanPage>(`/document-versions/${seg(dv)}/spans${qs({ cursor, limit })}`),
  async content(dv: string): Promise<ArrayBuffer> {
    const res = await raw(`/document-versions/${seg(dv)}/content`);
    return res.arrayBuffer();
  },
  getMapping: (dv: string) => json<MappingOut>(`/document-versions/${seg(dv)}/mapping`),
  confirmMapping: (dv: string, mapping: MappingRequest, reingest = true) =>
    json<MappingConfirmed>(`/document-versions/${seg(dv)}/mapping`, {
      json: { mapping, reingest },
      idempotent: true,
    }),
  /** 반영하지 못한 행·합계 차이를 확인했다고 기록(현재 버전, 같은 읽기 결과일 때만). */
  acknowledge: (dv: string, fingerprint: string) =>
    json<DocumentHead>(`/document-versions/${seg(dv)}/acknowledge`, {
      json: { fingerprint },
      idempotent: true,
    }),

  // jobs
  createJob: (type: JobType, params: Record<string, unknown> = {}) =>
    json<JobAccepted>("/jobs", { json: { type, params }, idempotent: true }),
  runAnalysis: (p: AnalysisParams) => {
    const params: Record<string, unknown> = {};
    if (p.as_of) params.as_of = p.as_of;
    if (p.rollover !== undefined && p.rollover !== null) params.rollover = p.rollover;
    if (p.rounding) params.rounding = p.rounding;
    return json<JobAccepted>("/jobs", {
      json: { type: "run_analysis", params },
      idempotent: true,
    });
  },
  job: (id: string, signal?: AbortSignal) => json<Job>(`/jobs/${seg(id)}`, { signal }),
  listJobs: (cursor?: string | null, limit = 20, status?: JobStatus) =>
    json<JobPage>(`/jobs${qs({ cursor, limit, status })}`),
  cancelJob: (id: string) => json<Job>(`/jobs/${seg(id)}/cancel`, { method: "POST" }),

  // decisions
  listDecisions: (f: DecisionFilter = {}) =>
    json<DecisionPage>(
      `/decisions${qs({
        status: f.status,
        review_status: f.review_status,
        subject_id: f.subject_id,
        cursor: f.cursor,
        limit: f.limit ?? 50,
      })}`,
    ),
  decision: (id: string) => json<DecisionDetail>(`/decisions/${seg(id)}`),
  approvals: (id: string) => json<{ items: Approval[] }>(`/decisions/${seg(id)}/approvals`),
  approve: (id: string, expectedResultHash: string) =>
    json<Approval>(`/decisions/${seg(id)}/approvals`, {
      json: { expected_result_hash: expectedResultHash },
      idempotent: true,
    }),

  /** 세금계산서가 정산 행과 같은 거래인지(same_sale) 별개 거래인지(separate_sale) 확인 기록. */
  confirmEvidenceLink: (id: string, expectedResultHash: string, body: EvidenceLinkRequest) =>
    json<LinkOutcome>(`/decisions/${seg(id)}/evidence-link`, {
      json: { expected_result_hash: expectedResultHash, ...body },
      idempotent: true,
    }),
  evidenceLinks: () => json<{ items: EvidenceLinkOut[] }>("/evidence-links"),
  withdrawEvidenceLink: (linkId: string) =>
    json<LinkOutcome>(`/evidence-links/${seg(linkId)}`, { method: "DELETE" }),

  // investigations (Agent 조사: 엔진 수치를 바꾸지 않는 참고 결과)
  investigationCapabilities: () => json<InvestigationCapabilities>("/investigations/capabilities"),
  async listInvestigations(decisionId: string, signal?: AbortSignal): Promise<Investigation[]> {
    const r = await json<Investigation[] | { items?: Investigation[] }>(
      `/decisions/${seg(decisionId)}/investigations`,
      { signal },
    );
    return Array.isArray(r) ? r : (r?.items ?? []);
  },
  startInvestigation: (
    decisionId: string,
    body: { strategy: InvestigationStrategy; mode: InvestigationMode },
  ) =>
    json<InvestigationAccepted>(`/decisions/${seg(decisionId)}/investigations`, {
      json: body,
      idempotent: true,
    }),

  // changes
  submitChanges: (changes: Record<string, unknown>[]) =>
    json<JobAccepted>("/changes", { json: { changes }, idempotent: true }),
  uploadChange(file: File, kind: DocKind, documentId?: string): Promise<ChangeUploadResponse> {
    const f = new FormData();
    f.append("file", file, file.name);
    f.append("kind", kind);
    if (documentId) f.append("document_id", documentId);
    return json<ChangeUploadResponse>("/changes/upload", { form: f, idempotent: true });
  },

  // reports
  async exportReport(req: {
    decision_ids?: string[] | null;
    format: "csv" | "html";
    require_approved: boolean;
  }): Promise<{ blob: Blob; filename: string }> {
    const res = await raw("/reports/export", { json: req });
    const cd = res.headers.get("content-disposition") ?? "";
    const m = /filename="([^"]+)"/.exec(cd);
    return { blob: await res.blob(), filename: m?.[1] ?? `jettae-report.${req.format}` };
  },

  // public
  async publicDue(req: PublicDueRequest): Promise<PublicDueResponse> {
    return normalizePublicDue(await json<unknown>("/public/due", { json: req, auth: false }));
  },

  // meta (인증 없음)
  async apiVersion(): Promise<string | null> {
    try {
      const res = await globalThis.fetch(url("/openapi.json"), { cache: "no-store" });
      if (!res.ok) return null;
      const doc = (await res.json()) as { info?: { version?: string } };
      return doc.info?.version ?? null;
    } catch {
      return null;
    }
  },
  health: () => json<{ status: string }>("/health", { auth: false }),
};

export async function fetchAllDecisions(
  f: Omit<DecisionFilter, "cursor"> = {},
  cap = 2000,
): Promise<{ items: DecisionSummary[]; truncated: boolean }> {
  const out: DecisionSummary[] = [];
  let cursor: string | null = null;
  do {
    const page: DecisionPage = await api.listDecisions({ ...f, cursor, limit: 200 });
    out.push(...page.items);
    cursor = page.next_cursor;
  } while (cursor && out.length < cap);
  return { items: out.slice(0, cap), truncated: Boolean(cursor) || out.length > cap };
}

/** 동시 요청 수를 제한해 목록의 각 항목에 대해 비동기 함수를 실행한다. 실패는 null. */
export async function mapLimit<T, R>(
  items: readonly T[],
  limit: number,
  fn: (x: T) => Promise<R>,
): Promise<(R | null)[]> {
  const out: (R | null)[] = new Array(items.length).fill(null);
  let next = 0;
  const workers = Array.from({ length: Math.min(limit, items.length) }, async () => {
    while (next < items.length) {
      const i = next++;
      try {
        out[i] = await fn(items[i]);
      } catch {
        out[i] = null;
      }
    }
  });
  await Promise.all(workers);
  return out;
}
