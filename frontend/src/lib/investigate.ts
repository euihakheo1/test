/**
 * Agent 조사 화면용 순수 함수(테스트 대상). 조사 결과는 참고 정보이며 엔진 수치를 바꾸지 않는다.
 * 서버 계약: POST/GET /api/v1/decisions/{id}/investigations, GET /api/v1/investigations/capabilities.
 */

import type {
  Investigation,
  InvestigationCapabilities,
  InvestigationMode,
  InvestigationStatus,
  InvestigationUsage,
} from "./api.ts";

export const INVESTIGATION_STATUS_LABEL: Record<InvestigationStatus, string> = {
  queued: "대기 중",
  running: "실행 중",
  succeeded: "완료",
  failed: "처리 실패",
  refused: "실행하지 않음(서버 설정)",
};

export const STRATEGY_LABEL: Record<string, string> = {
  single: "단일 Agent (single)",
  roles: "역할 분담 Agent (roles)",
};

export const MODE_LABEL: Record<string, string> = {
  offline: "오프라인 (규칙 기반 계획, LLM 호출 없음)",
  replay: "재생 (기록된 응답만 사용)",
  live: "실제 LLM 호출 (유료, 서버 예산 한도 안에서)",
};

export const FINDING_KIND_LABEL: Record<string, string> = {
  required_document: "필요 서류",
  unresolved_condition: "확인이 필요한 조건",
  difference: "차이",
  note: "참고",
};

/** 조사 결과가 계산 값과 별개임을 알리는 문구(화면·테스트가 같은 문장을 쓴다). */
export const NUMBERS_UNCHANGED_NOTE =
  "조사 결과는 참고 정보입니다. 위의 금액·지급기한·지연일수·지연이자는 엔진 계산 값이며 조사 결과로 바뀌지 않습니다.";

/**
 * 고를 수 있는 실행 방식. offline 은 항상 먼저 오고 기본값이다. live 는 서버가 live_enabled=true 일
 * 때만 보인다(예산·동의 설정이 없는 서버에서 유료 호출 선택지를 보여 주지 않기 위해서다).
 * 서버가 modes_enabled 를 비워 보내도 offline 은 LLM 호출이 없으므로 남긴다.
 */
export function modeOptions(c: InvestigationCapabilities | null | undefined): InvestigationMode[] {
  const enabled = new Set<string>(c?.modes_enabled ?? []);
  const out: InvestigationMode[] = ["offline"];
  if (enabled.has("replay")) out.push("replay");
  if (c?.live_enabled === true && enabled.has("live")) out.push("live");
  return out;
}

export function isActive(inv: Pick<Investigation, "status">): boolean {
  return inv.status === "queued" || inv.status === "running";
}

export function anyActive(list: readonly Pick<Investigation, "status">[]): boolean {
  return list.some(isActive);
}

/** 폴링 간격: 1초에서 시작해 1.4배씩, 최대 5초(작업 상태 화면과 같은 규칙). */
export function nextDelay(prev: number): number {
  return Math.min(5000, Math.round(prev * 1.4));
}

/** 사용량 문장. 비용은 정수 원; 정수가 아니면 원 단위로 꾸미지 않고 그대로 보여 준다. */
export function usageText(u: InvestigationUsage | null | undefined): string {
  if (!u) return "사용량 정보 없음";
  const cost = Number.isInteger(u.cost_krw) ? `${u.cost_krw.toLocaleString("ko-KR")}원` : `${u.cost_krw} KRW`;
  return `단계 ${u.steps} · 도구 호출 ${u.tool_calls} · 입력 토큰 ${u.input_tokens.toLocaleString("ko-KR")} · 출력 토큰 ${u.output_tokens.toLocaleString("ko-KR")} · 비용 ${cost}`;
}

export function errorMessage(e: Investigation["error"]): string | null {
  if (!e) return null;
  if (typeof e === "string") return e;
  return [e.code, e.message].filter(Boolean).join(": ") || null;
}

/** 모든 발견 사항의 필요 서류(중복 제거, 처음 나온 순서). */
export function requiredDocuments(inv: Pick<Investigation, "findings">): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const f of inv.findings ?? []) {
    for (const d of f.required_documents ?? []) {
      if (!seen.has(d)) {
        seen.add(d);
        out.push(d);
      }
    }
  }
  return out;
}

/** 최신 조사부터. created_at 이 같으면 서버 순서를 유지한다. */
export function newestFirst<T extends Pick<Investigation, "created_at">>(list: readonly T[]): T[] {
  return list
    .map((x, i) => ({ x, i }))
    .sort((a, b) => (a.x.created_at < b.x.created_at ? 1 : a.x.created_at > b.x.created_at ? -1 : a.i - b.i))
    .map((p) => p.x);
}
