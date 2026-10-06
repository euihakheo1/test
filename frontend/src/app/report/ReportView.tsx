"use client";

import Link from "next/link";
import { useState } from "react";
import { ReviewBadge, StatusBadge } from "@/components/Badge";
import { Loading, Msg } from "@/components/Msg";
import { useApi } from "@/components/useApi";
import { ApiError, api, fetchAllDecisions } from "@/lib/api";
import { errorText, kst, shortHash } from "@/lib/fmt";

export default function ReportView() {
  const list = useApi("all-decisions", () => fetchAllDecisions());
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [format, setFormat] = useState<"csv" | "html">("csv");
  const [requireApproved, setRequireApproved] = useState(false);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ kind: "ok" | "failure" | "pending"; text: string; invalid?: string[] } | null>(
    null,
  );

  const items = list.data?.items ?? [];

  async function exportNow() {
    setBusy(true);
    setMsg(null);
    try {
      const ids = picked.size ? [...picked] : null;
      const { blob, filename } = await api.exportReport({ decision_ids: ids, format, require_approved: requireApproved });
      const href = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = href;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(href), 10_000);
      setMsg({ kind: "ok", text: `${filename} 을(를) 내려받았습니다 (${kst(new Date().toISOString())} 기준 유효성 확인).` });
    } catch (e) {
      if (e instanceof ApiError && e.code === "report_not_valid") {
        // details.invalid: {decision_id: 현재 확인 상태}
        const raw = e.details?.invalid;
        const inv = Array.isArray(raw)
          ? raw.map(String)
          : raw && typeof raw === "object"
            ? Object.keys(raw as Record<string, unknown>)
            : [];
        setMsg({
          kind: "pending",
          text: `현재 확인(승인) 상태가 아닌 결과가 ${inv.length}건 있어 내보내지 않았습니다. 확인하거나 '확인된 결과만' 조건을 끄세요.`,
          invalid: inv,
        });
      } else {
        setMsg({ kind: "failure", text: errorText(e) });
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <div className="card">
        <p>
          내보내는 시점에 서버가 각 결과의 현재 유효성(확인 여부, 근거 변경)을 다시 검사해 보고서에 적습니다. CSV는
          엑셀 수식 실행을 막도록 처리됩니다.
        </p>
        <div className="row">
          <label className="inline">
            <input type="radio" checked={format === "csv"} onChange={() => setFormat("csv")} /> CSV
          </label>
          <label className="inline">
            <input type="radio" checked={format === "html"} onChange={() => setFormat("html")} /> HTML
          </label>
          <label className="inline">
            <input type="checkbox" checked={requireApproved} onChange={(e) => setRequireApproved(e.target.checked)} />
            현재 확인(승인)된 결과만 허용
          </label>
        </div>
        <p className="small" style={{ marginTop: "0.5rem" }}>
          대상: {picked.size ? `고른 결과 ${picked.size}건` : `전체 현재 결과${list.data ? ` ${items.length}건` : ""}`}
        </p>
        {msg && (
          <Msg kind={msg.kind} title={msg.kind === "pending" ? "확인 필요" : undefined}>
            {msg.text}
            {msg.invalid && msg.invalid.length > 0 && (
              <ul className="plain small">
                {msg.invalid.slice(0, 20).map((id) => (
                  <li key={id}>
                    <Link href={`/decision?id=${encodeURIComponent(id)}`} className="mono">
                      {id}
                    </Link>
                  </li>
                ))}
              </ul>
            )}
          </Msg>
        )}
        <button onClick={exportNow} disabled={busy || (list.data !== undefined && items.length === 0)}>
          {busy ? "만드는 중…" : "보고서 내려받기"}
        </button>
      </div>

      <h2>결과 고르기 (선택)</h2>
      {list.error ? <Msg kind="failure">{errorText(list.error)}</Msg> : null}
      {list.loading && <Loading />}
      {list.data && items.length === 0 && <Msg kind="empty">내보낼 결과가 없습니다. 먼저 분석을 실행하세요.</Msg>}
      {list.data?.truncated && <p className="hint">결과가 많아 처음 {items.length}건만 표시합니다.</p>}
      {items.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>
                  <input
                    type="checkbox"
                    aria-label="모두 고르기"
                    checked={picked.size === items.length}
                    onChange={(e) => setPicked(e.target.checked ? new Set(items.map((x) => x.id)) : new Set())}
                  />
                </th>
                <th>결과</th>
                <th>상태</th>
                <th>확인</th>
                <th>결과 해시</th>
              </tr>
            </thead>
            <tbody>
              {items.map((s) => (
                <tr key={s.id}>
                  <td>
                    <input
                      type="checkbox"
                      aria-label={`${s.subject_id} 고르기`}
                      checked={picked.has(s.id)}
                      onChange={(e) => {
                        const n = new Set(picked);
                        if (e.target.checked) n.add(s.id);
                        else n.delete(s.id);
                        setPicked(n);
                      }}
                    />
                  </td>
                  <td className="small">
                    <Link className="mono" href={`/decision?id=${encodeURIComponent(s.id)}`}>
                      {s.subject_id}
                    </Link>
                  </td>
                  <td>
                    <StatusBadge status={s.status} />
                  </td>
                  <td>
                    <ReviewBadge status={s.review_status} />
                  </td>
                  <td className="mono small">{shortHash(s.result_hash)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
