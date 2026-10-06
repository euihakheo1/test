"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useState } from "react";
import { ReviewBadge, StatusBadge } from "@/components/Badge";
import { DueCompact } from "@/components/DueView";
import { Loading, Msg } from "@/components/Msg";
import { useApi } from "@/components/useApi";
import { api, mapLimit, type DecisionDetail, type DecisionSummary } from "@/lib/api";
import {
  MISSING_LABEL,
  REVIEW_LABEL,
  STATUS_LABEL,
  UNRESOLVED_LABEL,
  errorText,
  isPendingStatus,
  label,
  shortHash,
  won,
} from "@/lib/fmt";
import { keyNumbers, subjectLabel } from "@/lib/view";

const STATUSES = Object.keys(STATUS_LABEL);
const REVIEWS = Object.keys(REVIEW_LABEL);
const PAGE = 50;

interface Row {
  s: DecisionSummary;
  d: DecisionDetail | null;
}

export default function ResultsView() {
  const params = useSearchParams();
  const router = useRouter();
  const status = params.getAll("status").filter((x) => STATUSES.includes(x));
  const review = params.getAll("review_status").filter((x) => REVIEWS.includes(x));
  const cursor = params.get("cursor");
  const key = `${status.join(",")}|${review.join(",")}|${cursor ?? ""}`;

  const page = useApi(key, async () => {
    const p = await api.listDecisions({ status, review_status: review, cursor, limit: PAGE });
    // 목록 응답에는 금액·기한이 없어서 각 결과의 상세를 함께 읽는다(동시 4개).
    const details = await mapLimit(p.items, 4, (s) => api.decision(s.id));
    const rows: Row[] = p.items.map((s, i) => ({ s, d: details[i] }));
    return { rows, next: p.next_cursor };
  });

  const [history, setHistory] = useState<(string | null)[]>([]);

  function go(next: { status?: string[]; review?: string[]; cursor?: string | null }) {
    const u = new URLSearchParams();
    (next.status ?? status).forEach((x) => u.append("status", x));
    (next.review ?? review).forEach((x) => u.append("review_status", x));
    if (next.cursor) u.set("cursor", next.cursor);
    router.push(`/results${u.size ? `?${u}` : ""}`);
  }
  function toggle(list: string[], v: string): string[] {
    return list.includes(v) ? list.filter((x) => x !== v) : [...list, v];
  }

  const rows = page.data?.rows ?? [];
  const pending = rows.filter((r) => isPendingStatus(r.s.status)).length;
  const failedDetails = rows.filter((r) => r.d === null).length;

  return (
    <>
      <div className="card">
        <div className="row small">
          <b>대사 상태</b>
          {STATUSES.map((s) => (
            <label key={s} className="inline">
              <input
                type="checkbox"
                checked={status.includes(s)}
                onChange={() => {
                  setHistory([]);
                  go({ status: toggle(status, s), cursor: null });
                }}
              />
              {STATUS_LABEL[s]}
            </label>
          ))}
        </div>
        <div className="row small" style={{ marginTop: "0.4rem" }}>
          <b>확인 상태</b>
          {REVIEWS.map((s) => (
            <label key={s} className="inline">
              <input
                type="checkbox"
                checked={review.includes(s)}
                onChange={() => {
                  setHistory([]);
                  go({ review: toggle(review, s), cursor: null });
                }}
              />
              {REVIEW_LABEL[s]}
            </label>
          ))}
        </div>
        <p className="hint">
          &lsquo;기계 검사 통과&rsquo;는 숫자·인용·문구 검사를 통과했다는 뜻이며 법적 판단이 아닙니다. 차액은 정산
          금액에서 연결된 입금 배분액을 뺀 미결 금액입니다.
        </p>
      </div>

      {page.error ? <Msg kind="failure">{errorText(page.error)}</Msg> : null}
      {page.loading && <Loading text="결과와 상세를 불러오는 중" />}
      {!page.loading && page.data && rows.length === 0 && (
        <Msg kind="empty">
          {status.length || review.length
            ? "조건에 맞는 결과가 없습니다."
            : "아직 분석 결과가 없습니다. 문서를 올리고 분석을 실행하세요."}{" "}
          <Link href="/upload">문서 업로드</Link> · <Link href="/analysis">분석</Link>
        </Msg>
      )}
      {!page.loading && pending > 0 && (
        <Msg kind="pending">
          이 페이지의 {pending}건은 근거 부족·후보 여러 개·조건 충돌로 판단을 보류했습니다. 필요 서류와 확인할 조건을
          확인하세요.
        </Msg>
      )}
      {!page.loading && failedDetails > 0 && (
        <Msg kind="failure">상세를 읽지 못한 결과가 {failedDetails}건 있습니다(금액·기한 칸이 비어 있음).</Msg>
      )}

      {!page.loading && rows.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>거래</th>
                <th>상태</th>
                <th className="num">금액 / 입금 배분</th>
                <th className="num">차액(미결)</th>
                <th>지급기한 · 지연일수 · 지연이자</th>
                <th>계산 가정 · 확인할 조건</th>
                <th>필요 서류</th>
              </tr>
            </thead>
            <tbody>
              {rows.map(({ s, d }) => {
                const k = d ? keyNumbers(d) : null;
                return (
                  <tr key={s.id}>
                    <td>
                      <Link href={`/decision?id=${encodeURIComponent(s.id)}`}>{d ? subjectLabel(d) : s.subject_id}</Link>
                      <div className="hint mono" title={s.result_hash}>
                        결과 {shortHash(s.result_hash)}
                      </div>
                      {s.counted === false && (
                        <div>
                          <span className="badge b-warn">확인 대기</span>{" "}
                          <span className="hint">같은 거래인지 확인 전: 미수 합계 제외</span>
                        </div>
                      )}
                    </td>
                    <td>
                      <StatusBadge status={s.status} />
                      <div style={{ marginTop: 4 }}>
                        <ReviewBadge status={s.review_status} />
                      </div>
                    </td>
                    <td className="num">
                      {k ? won(k.amount) : "-"}
                      <div className="hint">{k ? won(k.allocated) : ""}</div>
                    </td>
                    <td className="num">
                      {s.counted === false ? (
                        <span className="hint">합계 제외</span>
                      ) : k?.open ? (
                        <b>{won(k.open)}</b>
                      ) : (
                        "-"
                      )}
                      {k?.feeDifference && k.feeDifference.amount !== 0 ? (
                        <div className="hint">허용 오차 {won(k.feeDifference)}</div>
                      ) : null}
                    </td>
                    <td>{k ? <DueCompact due={k.due} /> : "-"}</td>
                    <td className="small">
                      {d?.assumptions.length ? (
                        <details>
                          <summary>가정 {d.assumptions.length}개</summary>
                          <ul className="plain">
                            {d.assumptions.map((a, i) => (
                              <li key={i}>{a}</li>
                            ))}
                          </ul>
                        </details>
                      ) : null}
                      {s.unresolved.length > 0 && (
                        <div>
                          <span className="badge b-warn">확인 필요</span>{" "}
                          {s.unresolved.map((u) => label(UNRESOLVED_LABEL, u)).join(", ")}
                        </div>
                      )}
                      {s.missing.length > 0 && (
                        <div className="hint">미확인 항목: {s.missing.map((m) => label(MISSING_LABEL, m)).join(", ")}</div>
                      )}
                    </td>
                    <td className="small">
                      {s.required_documents.length ? (
                        <ul className="plain">
                          {s.required_documents.map((r, i) => (
                            <li key={i}>{r}</li>
                          ))}
                        </ul>
                      ) : (
                        <span className="muted">-</span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      <p className="row" style={{ marginTop: "0.75rem" }}>
        {history.length > 0 && (
          <button
            className="secondary"
            onClick={() => {
              const prev = history[history.length - 1];
              setHistory(history.slice(0, -1));
              go({ cursor: prev });
            }}
          >
            이전 페이지
          </button>
        )}
        {page.data?.next && (
          <button
            className="secondary"
            onClick={() => {
              setHistory([...history, cursor]);
              go({ cursor: page.data!.next });
            }}
          >
            다음 페이지
          </button>
        )}
        <button className="secondary" onClick={page.reload}>
          새로고침
        </button>
        <Link className="btn secondary" href="/report">
          보고서 내보내기
        </Link>
      </p>
    </>
  );
}
