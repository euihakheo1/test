import type { DueVariant } from "@/lib/api";
import { MISSING_LABEL, TRADE_TYPE_LABEL, UNRESOLVED_LABEL, VARIANT_LABEL, label, won } from "@/lib/fmt";
import type { DueInfo } from "@/lib/view";

/** 목록 칸용 간단 표시: 변형별 지급기한·최대 지연일수·지연이자. */
export function DueCompact({ due }: { due: DueInfo }) {
  if (due.state === "none") return <span className="muted">계산 없음</span>;
  if (due.state === "insufficient") {
    return (
      <span>
        <span className="badge b-neutral">계산 보류</span>
        <div className="hint">미확인: {due.missing.map((m) => label(MISSING_LABEL, m)).join(", ") || "-"}</div>
      </span>
    );
  }
  return (
    <div>
      {due.variants.map((v) => (
        <div key={v.label} className="small">
          {due.variants.length > 1 && <span className="muted">{label(VARIANT_LABEL, v.label)}: </span>}
          기한 <b>{v.due_date}</b> · {v.max_delay_days === null ? "지연·이자 미계산(입금 배분 미확정)" : <>지연 {v.max_delay_days}일 · 이자 {won(v.interest_total)}</>}
        </div>
      ))}
      {due.unresolved.length > 0 && (
        <div className="hint">확인 필요: {due.unresolved.map((u) => label(UNRESOLVED_LABEL, u)).join(", ")}</div>
      )}
    </div>
  );
}

/** 상세용: 변형별 표 + 분할 지급(tranche)별 지연일수·이자. */
export function DueTable({ due }: { due: DueInfo }) {
  if (due.state === "none") return <p className="muted">이 결과에는 지급기한 계산이 없습니다.</p>;
  return (
    <>
      <dl className="kv">
        <dt>거래 형태</dt>
        <dd>{label(TRADE_TYPE_LABEL, due.tradeType)}</dd>
        <dt>기준일</dt>
        <dd>{due.baseDate ?? "미확인"}</dd>
        {due.termDays !== null && (
          <>
            <dt>기한 일수</dt>
            <dd>{due.termDays}일</dd>
          </>
        )}
        <dt>계산 기준일(as_of)</dt>
        <dd>{due.asOf ?? "-"}</dd>
        <dt>원 단위 처리</dt>
        <dd>{due.rounding === "half_up" ? "반올림" : due.rounding === "floor" ? "버림" : (due.rounding ?? "-")}</dd>
        <dt>휴일 이월 설정</dt>
        <dd>{due.rolloverSetting === null ? "정하지 않음(두 계산 표시)" : due.rolloverSetting ? "이월" : "말일 그대로"}</dd>
      </dl>
      {due.state === "insufficient" ? null : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>계산 방식</th>
                <th>지급기한</th>
                <th className="num">최대 지연일수</th>
                <th className="num">지연이자 합계</th>
                <th>구간별(금액 · 종료일 · 지연일수 · 이자)</th>
              </tr>
            </thead>
            <tbody>
              {due.variants.map((v: DueVariant) => (
                <tr key={v.label}>
                  <td>{label(VARIANT_LABEL, v.label)}</td>
                  <td className="nowrap">{v.due_date}</td>
                  <td className="num">{v.max_delay_days === null ? "미계산" : `${v.max_delay_days}일`}</td>
                  <td className="num">{won(v.interest_total)}</td>
                  <td className="small">
                    {v.tranches?.length
                      ? v.tranches.map((t, i) => (
                          <div key={i}>
                            {won(t.amount)} · {t.end_date} · {t.delay_days}일 · {won(t.interest)}{" "}
                            <span className="muted">({t.source === "unpaid" ? "미지급분, 기준일까지" : t.source})</span>
                          </div>
                        ))
                      : "-"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {due.unresolved.length > 0 && (
        <p className="small">
          <span className="badge b-warn">확인이 필요한 조건</span>{" "}
          {due.unresolved.map((u) => label(UNRESOLVED_LABEL, u)).join(", ")} — 조건이 확인되기 전까지 두 계산을 함께
          보여 줍니다.
        </p>
      )}
    </>
  );
}
