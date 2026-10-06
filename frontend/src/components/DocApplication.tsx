"use client";

import { useState } from "react";
import type { ApplyOutcome, RowCounts, RowIssue, TotalCheck } from "@/lib/api";
import { api } from "@/lib/api";
import {
  ISSUE_KIND_LABEL,
  applyMsgKind,
  applyStateLabel,
  applyStateTone,
  applyTitle,
  canAcknowledge,
  carriedOverText,
  countsText,
  mismatchedTotals,
} from "@/lib/apply";
import { errorText, won } from "@/lib/fmt";
import { canWrite } from "@/lib/session";
import { Msg } from "./Msg";
import { useSession } from "./useSession";

export function ApplyBadge({ state }: { state: string | null | undefined }) {
  return <span className={`badge ${applyStateTone(state)}`}>{applyStateLabel(state)}</span>;
}

/**
 * 문서 버전을 장부에 반영한 결과: 반영/제외 행 수, 행별 문제, 합계 확인, 빈 정정본 제거 건수,
 * 이전 버전 안내, 제외 행 확인(승인 전 필요) 버튼.
 */
export function DocApplication({
  outcome,
  counts,
  issues,
  issuesTotal,
  totals,
  isCurrent,
  onAcknowledged,
}: {
  outcome: ApplyOutcome;
  counts: RowCounts | null | undefined;
  issues: RowIssue[];
  issuesTotal?: number;
  totals: TotalCheck[];
  isCurrent: boolean;
  onAcknowledged?: () => void;
}) {
  const session = useSession();
  const [busy, setBusy] = useState(false);
  const [ackError, setAckError] = useState<string | null>(null);
  const [acked, setAcked] = useState(outcome.acknowledged);
  const [removedOnAck, setRemovedOnAck] = useState<number | null>(null);
  const o = { ...outcome, acknowledged: acked };
  const bad = mismatchedTotals(totals);
  const total = issuesTotal ?? issues.length;

  async function acknowledge() {
    setBusy(true);
    setAckError(null);
    try {
      const head = await api.acknowledge(o.doc_version_id, o.fingerprint);
      setRemovedOnAck(head.records_removed ?? 0);
      setAcked(true);
      onAcknowledged?.();
    } catch (e) {
      setAckError(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card">
      <h3 style={{ marginTop: 0 }}>
        장부 반영 결과 <ApplyBadge state={o.state} />
      </h3>
      <Msg kind={applyMsgKind(o)} title={applyTitle(o)}>
        {o.message}
      </Msg>
      <dl className="kv">
        <dt>읽은 행</dt>
        <dd>{countsText(counts)}</dd>
        <dt>장부 변경</dt>
        <dd>
          추가 {o.records_added}건 · 갱신 {o.records_updated}건 · 제거 {o.records_removed}건
          {(o.records_carried_over ?? 0) > 0 && <> · 이전 버전 기록 유지 {o.records_carried_over}건</>}
          {removedOnAck !== null && removedOnAck > 0 && <> · 확인으로 제거 {removedOnAck}건</>}
        </dd>
        <dt>현재 반영 버전</dt>
        <dd>
          {o.current_version ? `v${o.current_version}` : "없음"}
          {o.current_version !== null && o.current_version !== o.version ? (
            <span className="hint"> (이 문서는 v{o.version})</span>
          ) : null}
        </dd>
      </dl>

      {carriedOverText(o) && (
        <Msg kind="pending" title="이전 버전 기록 유지">
          {carriedOverText(o)}
        </Msg>
      )}

      {o.state === "applied_empty" && (
        <Msg kind="pending" title="빈 정정본 확인">
          표 머리글은 있지만 거래 행이 0개인 정정본입니다. 이 문서의 이전 기록 {o.records_removed}건을 장부에서
          제거했습니다. 의도한 정정이 아니라면 올바른 파일을 새 버전으로 다시 올리세요.
        </Msg>
      )}

      {total > 0 && (
        <>
          <h4>행별 문제 ({total}건)</h4>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>표</th>
                  <th className="num">행</th>
                  <th>항목</th>
                  <th>구분</th>
                  <th>내용</th>
                </tr>
              </thead>
              <tbody>
                {issues.map((i, n) => (
                  <tr key={`${i.table}:${i.row}:${i.field ?? "-"}:${n}`}>
                    <td className="mono small">{i.table}</td>
                    <td className="num">{i.row}</td>
                    <td className="mono small">{i.field ?? "-"}</td>
                    <td>
                      <span className={`badge ${i.kind === "excluded" ? "b-bad" : "b-warn"}`}>
                        {ISSUE_KIND_LABEL[i.kind] ?? i.kind}
                      </span>
                    </td>
                    <td className="small">{i.message}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {total > issues.length && <p className="hint">처음 {issues.length}건만 표시합니다.</p>}
        </>
      )}

      {totals.length > 0 && (
        <>
          <h4>합계 확인</h4>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>표</th>
                  <th className="num">합계 행</th>
                  <th>항목</th>
                  <th className="num">표의 합계</th>
                  <th className="num">행 합계</th>
                  <th>결과</th>
                </tr>
              </thead>
              <tbody>
                {totals.map((t) => (
                  <tr key={`${t.table}:${t.row}:${t.field}`}>
                    <td className="mono small">{t.table}</td>
                    <td className="num">{t.row}</td>
                    <td className="mono small">{t.field}</td>
                    <td className="num">{won(t.stated)}</td>
                    <td className="num">{won(t.computed)}</td>
                    <td>
                      <span className={`badge ${t.matches ? "b-ok" : "b-bad"}`}>{t.matches ? "같음" : "다름"}</span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {bad.length > 0 && (
            <p className="hint">
              표의 합계와 읽은 행의 합계가 다릅니다. 읽지 못한 행이 있거나 원본 합계가 다를 수 있습니다.
            </p>
          )}
        </>
      )}

      {o.ack_required &&
        (o.acknowledged ? (
          <Msg kind="ok" title="확인됨">
            제외된 행과 합계 차이를 확인했습니다. 이 문서를 근거로 한 결과를 승인할 수 있습니다.
          </Msg>
        ) : (
          <Msg kind="pending" title="승인 전 확인 필요">
            이 문서를 근거로 한 결과는 위의 제외 행·합계 차이를 확인하기 전까지 승인할 수 없습니다.
          </Msg>
        ))}
      {canAcknowledge(o, isCurrent) && canWrite(session?.role) && (
        <button type="button" disabled={busy} onClick={acknowledge}>
          {busy ? "기록 중…" : "제외 행·합계 차이를 확인했습니다"}
        </button>
      )}
      {ackError && <Msg kind="failure">{ackError}</Msg>}
    </div>
  );
}
