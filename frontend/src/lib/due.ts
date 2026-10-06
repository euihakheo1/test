/**
 * 무료 검산(공개) 응답 정규화. 백엔드가 to_plain 형식({amount,currency}, ISO 날짜)으로 주든,
 * CLI `jettae rules due --json` 의 정규 형식({"$type"}, {"$date"}, {"$money": [금액, 통화]})으로 주든
 * 같은 모양(PublicDueResponse)으로 바꾼다. 숫자를 새로 계산하지 않는다.
 */

import type { Money, PublicDueRequest, PublicDueResponse } from "./api.ts";
import { isIsoDate } from "./fmt.ts";

type J = unknown;

function unwrap(v: J): J {
  if (Array.isArray(v)) return v.map(unwrap);
  if (v && typeof v === "object") {
    const o = v as Record<string, J>;
    if ("$date" in o && Object.keys(o).length === 1) return o.$date;
    if ("$money" in o && Array.isArray(o.$money) && Object.keys(o).length === 1) {
      const [amount, currency] = o.$money as [J, J];
      return { amount, currency };
    }
    const out: Record<string, J> = {};
    for (const [k, x] of Object.entries(o)) out[k] = unwrap(x);
    return out;
  }
  return v;
}

const isMoneyLike = (v: J): v is Money =>
  !!v && typeof v === "object" && Number.isInteger((v as Money).amount) && typeof (v as Money).currency === "string";

export class DueFormatError extends Error {}

export function normalizePublicDue(body: J): PublicDueResponse {
  const o = unwrap(body);
  if (!o || typeof o !== "object") throw new DueFormatError("응답 형식이 올바르지 않습니다");
  const r = o as Record<string, J>;
  const type = r.$type ?? r.result;
  const strs = (v: J): string[] => (Array.isArray(v) ? v.map(String) : []);
  if (type === "Insufficient" || type === "insufficient" || (!("due_date" in r) && "required_documents" in r)) {
    return {
      result: "insufficient",
      missing: strs(r.missing),
      required_documents: strs(r.required_documents),
      notes: strs(r.notes),
    };
  }
  if (typeof r.due_date !== "string") throw new DueFormatError("응답에 지급기한이 없습니다");
  const money = (v: J): Money | null => {
    if (v === null || v === undefined) return null;
    if (!isMoneyLike(v)) throw new DueFormatError("금액이 정수(원)가 아닙니다");
    return v;
  };
  const variants = (Array.isArray(r.variants) ? r.variants : []).map((x) => {
    const v = x as Record<string, J>;
    return {
      label: String(v.label),
      rollover: Boolean(v.rollover),
      due_date: String(v.due_date),
      delay_days: Number(v.delay_days),
      interest: money(v.interest),
    };
  });
  return {
    result: "due",
    base_date: String(r.base_date),
    due_date: r.due_date,
    delay_days: Number(r.delay_days),
    interest: money(r.interest),
    assumptions: strs(r.assumptions),
    variants,
    rule_version: String(r.rule_version ?? ""),
    interest_rule_version: typeof r.interest_rule_version === "string" ? r.interest_rule_version : null,
    rounding: String(r.rounding ?? ""),
    unresolved: strs(r.unresolved),
    principal: money(r.principal) ?? { amount: 0, currency: "KRW" },
    end_date: typeof r.end_date === "string" ? r.end_date : null,
    trade_type: String(r.trade_type ?? ""),
    term_days: Number(r.term_days),
    source_urls: strs(r.source_urls),
  };
}

/** 무료 검산 입력 검사. 문제 목록(빈 배열이면 통과). 기준일은 비워 둘 수 있다(서버가 필요 서류 안내). */
export function validateDueInput(x: PublicDueRequest): string[] {
  const p: string[] = [];
  if (!["direct", "consignment", "subcontract"].includes(x.trade_type)) p.push("거래 유형을 고르세요.");
  if (x.base_date && !isIsoDate(x.base_date)) p.push("기준일 형식이 올바르지 않습니다.");
  if (x.paid_date && !isIsoDate(x.paid_date)) p.push("지급일 형식이 올바르지 않습니다.");
  if (x.as_of && !isIsoDate(x.as_of)) p.push("계산 기준일 형식이 올바르지 않습니다.");
  if (x.base_date && x.paid_date && x.paid_date < x.base_date) p.push("지급일이 기준일보다 앞섭니다.");
  if (!Number.isSafeInteger(x.amount) || x.amount < 0) p.push("금액은 0 이상의 정수(원)여야 합니다.");
  return p;
}
