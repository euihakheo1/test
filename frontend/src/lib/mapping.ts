/**
 * 열 매핑 화면의 상태 계산(순수 함수).
 *
 * 서버 계약(POST /document-versions/{id}/mapping, jettae.app.contracts.MappingRequest):
 *   {mapping: {format_id?, columns: {항목: 열 번호 | null}, options: {counterparty_override?, ...}}}
 * - columns 는 항상 열 번호(0부터)다. null 은 "사용자가 이 항목의 열을 지웠다"는 뜻이다.
 * - options 는 모든 행에 적용할 값(문자열)이다. 열 번호를 options 에 넣지 않는다.
 *   예: 거래처 열이 두 번째 열이면 columns.counterparty = 1,
 *       문서에 거래처 열이 없어 이름을 직접 적으면 options.counterparty_override = "가나유통".
 * - table 은 열 번호가 가리키는 표(제안의 suggestion.table)다. 열 번호는 한 표의 머리글에서만
 *   의미가 있으므로, 서버는 매핑을 그 표(와 머리글이 똑같은 아직 인식되지 않은 표)에만 적용한다.
 *   표가 여러 개인 파일에서 table 없이 보내면 서버가 거부할 수 있다.
 */

import type { MappingOptions, MappingOut, MappingRequest } from "./api.ts";

/** 화면에서 입력받는 문서 단위 값(텍스트). */
export const OPTION_KEYS = ["counterparty_override", "account_override", "self_brn", "direction"] as const;
export type OptionKey = (typeof OPTION_KEYS)[number];
export type OptionValues = Partial<Record<OptionKey, string>>;

/** field → 열 번호(0부터) 또는 null(매핑 안 함). */
export type Selection = Record<string, number | null>;

export function headerIndex(headers: string[], v: number | string | null | undefined): number | null {
  if (typeof v === "number") return Number.isInteger(v) && v >= 0 && v < headers.length ? v : null;
  if (typeof v === "string") {
    const i = headers.indexOf(v);
    return i >= 0 ? i : null;
  }
  return null;
}

/** 확정된 매핑이 있으면 그것을, 없으면 제안 매핑을 시작값으로 쓴다. */
export function initialSelection(m: MappingOut): {
  selection: Selection;
  options: OptionValues;
  formatId: string | null;
  table: string | null;
} {
  const s = m.suggestion;
  const headers = s?.headers ?? [];
  const fields = s?.fields ?? [];
  const selection: Selection = {};
  for (const f of fields) selection[f.name] = null;
  const options: OptionValues = {};
  const confirmed = m.confirmed?.mapping;
  // 확정 매핑이 다른 표의 것이면(예: 첫 표를 확정한 뒤 두 번째 표가 제안됨) 그 열 번호를 쓰지 않는다.
  const sameTable = !confirmed?.table || !s?.table || confirmed.table === s.table;
  if (confirmed && sameTable) {
    for (const [k, v] of Object.entries(confirmed.columns ?? {})) selection[k] = headerIndex(headers, v);
    for (const k of OPTION_KEYS) {
      const v = confirmed.options?.[k];
      if (typeof v === "string" && v) options[k] = v;
    }
    return {
      selection,
      options,
      formatId: confirmed.format_id ?? s?.format_id ?? null,
      table: s?.table ?? confirmed.table ?? null,
    };
  }
  for (const match of s?.matches ?? []) {
    if (match.confidence === "none") continue;
    const idx = headerIndex(headers, match.column ?? match.header);
    if (idx !== null) selection[match.field] = idx;
  }
  return { selection, options, formatId: s?.format_id ?? null, table: s?.table ?? null };
}

/**
 * 보낼 매핑을 만든다. 고른 열은 열 번호로, 시작값에 있었는데 사용자가 지운 항목은 null 로
 * 보낸다(자동 판별이 다시 그 열을 잡지 않도록). 빈 옵션은 보내지 않는다.
 */
export function buildMapping(
  selection: Selection,
  initial: Selection,
  options: OptionValues,
  formatId: string | null = null,
  table: string | null = null,
): MappingRequest {
  const columns: Record<string, number | null> = {};
  for (const [field, idx] of Object.entries(selection)) {
    if (idx !== null && Number.isInteger(idx) && idx >= 0) columns[field] = idx;
    else if (initial[field] !== null && initial[field] !== undefined) columns[field] = null;
  }
  const opts: MappingOptions = {};
  for (const k of OPTION_KEYS) {
    const v = options[k]?.trim();
    if (!v) continue;
    if (k === "direction") {
      if (v === "sales" || v === "purchase") opts.direction = v;
    } else {
      opts[k] = v;
    }
  }
  const out: MappingRequest = { columns, options: opts };
  if (formatId) out.format_id = formatId;
  if (table) out.table = table;
  return out;
}

/** 같은 열을 두 항목에 고른 경우 → 열 번호 목록. */
export function duplicateColumns(selection: Selection): number[] {
  const seen = new Map<number, number>();
  for (const v of Object.values(selection)) if (v !== null) seen.set(v, (seen.get(v) ?? 0) + 1);
  return [...seen.entries()].filter(([, n]) => n > 1).map(([c]) => c);
}

export function missingRequired(m: MappingOut, selection: Selection): string[] {
  return (m.suggestion?.fields ?? []).filter((f) => f.required && selection[f.name] == null).map((f) => f.name);
}
