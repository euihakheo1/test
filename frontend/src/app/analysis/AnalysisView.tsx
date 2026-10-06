"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";
import { JobBadge } from "@/components/Badge";
import { Loading, Msg } from "@/components/Msg";
import { useApi } from "@/components/useApi";
import { useSession } from "@/components/useSession";
import { api, type JobPage } from "@/lib/api";
import { JOB_TYPE_LABEL, errorText, isIsoDate, kst, label } from "@/lib/fmt";
import { canWrite } from "@/lib/session";

type Roll = "unset" | "on" | "off";

export default function AnalysisView() {
  const router = useRouter();
  const session = useSession();
  const [asOf, setAsOf] = useState("");
  const [roll, setRoll] = useState<Roll>("unset");
  const [rounding, setRounding] = useState<"floor" | "half_up">("floor");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const jobs = useApi<JobPage>("jobs", () => api.listJobs(null, 20));

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (asOf && !isIsoDate(asOf)) {
      setError("기준일 형식이 올바르지 않습니다(YYYY-MM-DD).");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const r = await api.runAnalysis({
        as_of: asOf || null,
        rollover: roll === "unset" ? null : roll === "on",
        rounding,
      });
      router.push(`/job?id=${encodeURIComponent(r.job_id)}`);
    } catch (err) {
      setError(errorText(err));
      setBusy(false);
    }
  }

  return (
    <>
      {canWrite(session?.role) ? (
        <form className="card" onSubmit={submit}>
          <p>
            올린 문서의 거래·입금을 연결하고, 기준일이 확인된 거래의 지급기한·지연일수·지연이자를 계산합니다. 문서를
            올리거나 정정하면 영향받은 거래는 자동으로 다시 계산되며, 이 화면은 설정을 바꿔 전체를 다시 계산할 때 씁니다.
          </p>
          <div className="grid2">
            <div className="field">
              <label htmlFor="asof">계산 기준일(as_of)</label>
              <input id="asof" type="date" value={asOf} onChange={(e) => setAsOf(e.target.value)} />
              <p className="hint">지급되지 않은 금액의 지연일수를 이 날짜까지 셉니다. 비우면 오늘(한국 날짜).</p>
            </div>
            <div className="field">
              <label htmlFor="roll">기한 말일이 휴일일 때</label>
              <select id="roll" value={roll} onChange={(e) => setRoll(e.target.value as Roll)}>
                <option value="unset">정하지 않음 — 두 계산을 함께 표시</option>
                <option value="off">말일 그대로</option>
                <option value="on">다음 영업일로 이월</option>
              </select>
              <p className="hint">약정 등으로 확인되지 않았다면 &lsquo;정하지 않음&rsquo;을 권장합니다.</p>
            </div>
            <div className="field">
              <label htmlFor="rnd">원 단위 처리</label>
              <select id="rnd" value={rounding} onChange={(e) => setRounding(e.target.value as "floor" | "half_up")}>
                <option value="floor">버림</option>
                <option value="half_up">반올림</option>
              </select>
            </div>
          </div>
          {error && <Msg kind="failure">{error}</Msg>}
          <button type="submit" disabled={busy}>
            {busy ? "요청 중…" : "분석 실행"}
          </button>
        </form>
      ) : (
        <Msg kind="info">보기 전용 역할은 분석을 실행할 수 없습니다.</Msg>
      )}

      <h2>최근 작업</h2>
      <p>
        <button className="secondary" onClick={jobs.reload}>
          새로고침
        </button>
      </p>
      {jobs.error ? <Msg kind="failure">{errorText(jobs.error)}</Msg> : null}
      {jobs.loading && !jobs.data && <Loading />}
      {jobs.data && jobs.data.items.length === 0 && <Msg kind="empty">아직 작업이 없습니다.</Msg>}
      {jobs.data && jobs.data.items.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>종류</th>
                <th>상태</th>
                <th>만든 시각</th>
                <th>끝난 시각</th>
                <th>요청자</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {jobs.data.items.map((j) => (
                <tr key={j.id}>
                  <td>{label(JOB_TYPE_LABEL, j.type)}</td>
                  <td>
                    <JobBadge status={j.status} />
                  </td>
                  <td className="nowrap">{kst(j.created_at)}</td>
                  <td className="nowrap">{kst(j.finished_at)}</td>
                  <td>{j.created_by ?? "-"}</td>
                  <td>
                    <Link href={`/job?id=${encodeURIComponent(j.id)}`}>보기</Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
