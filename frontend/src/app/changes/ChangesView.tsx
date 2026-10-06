"use client";

import Link from "next/link";
import { useState, type FormEvent } from "react";
import { JobBadge, ReviewBadge, StatusBadge } from "@/components/Badge";
import { DropZone } from "@/components/DropZone";
import { ImpactSummary, JobResult } from "@/components/JobResult";
import { KindSelect } from "@/components/KindSelect";
import { Loading, Msg } from "@/components/Msg";
import { useApi } from "@/components/useApi";
import { TERMINAL, useJobPoll } from "@/components/useJobPoll";
import { useSession } from "@/components/useSession";
import {
  api,
  fetchAllDecisions,
  mapLimit,
  type ChangeOutcome,
  type ChangeUploadResponse,
  type DocKind,
  type DocumentPage,
  type IngestJobResult,
  type Job,
} from "@/lib/api";
import { MISSING_LABEL, errorText, kst, label, won } from "@/lib/fmt";
import { canWrite } from "@/lib/session";
import { compare, snapshotOf, type CompareRow, type Snapshot } from "@/lib/view";

/** 이전 결과를 상세까지 보관하는 최대 건수(브라우저 메모리·요청 수 제한). */
const BEFORE_DETAIL_CAP = 200;
const AFTER_DETAIL_CAP = 200;

interface Before {
  at: string;
  total: number;
  truncated: boolean;
  snaps: Map<string, Snapshot>;
  detailed: number;
}

type Phase = "idle" | "snapshot" | "upload" | "running";

export default function ChangesView() {
  const session = useSession();
  const [file, setFile] = useState<File | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [kind, setKind] = useState<DocKind>("other");
  const [documentId, setDocumentId] = useState("");
  const [phase, setPhase] = useState<Phase>("idle");
  const [error, setError] = useState<string | null>(null);
  const [before, setBefore] = useState<Before | null>(null);
  const [upload, setUpload] = useState<ChangeUploadResponse | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);

  const docs = useApi<DocumentPage>("docs", () => api.listDocuments(null, 200));
  const { job } = useJobPoll(jobId);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!file || problem) return;
    setError(null);
    setUpload(null);
    setJobId(null);
    setBefore(null);
    try {
      // 1) 올리기 전 결과를 보관(이전/현재 비교용)
      setPhase("snapshot");
      const all = await fetchAllDecisions();
      const ids = all.items.map((s) => s.id).slice(0, BEFORE_DETAIL_CAP);
      const details = await mapLimit(ids, 6, (id) => api.decision(id));
      const snaps = new Map<string, Snapshot>();
      details.forEach((d) => {
        if (d) snaps.set(d.id, snapshotOf(d, won));
      });
      // 상세를 못 읽은 결과도 요약(상태·해시)은 보관
      for (const s of all.items) {
        if (!snaps.has(s.id)) {
          snaps.set(s.id, {
            status: s.status,
            review_status: s.review_status,
            result_hash: s.result_hash,
            open: null,
            interest: "(보관 안 함)",
            due: "(보관 안 함)",
            missing: s.missing,
            required_documents: s.required_documents,
          });
        }
      }
      setBefore({
        at: new Date().toISOString(),
        total: all.items.length,
        truncated: all.truncated,
        snaps,
        detailed: details.filter(Boolean).length,
      });
      // 2) 파일 올리기 → 읽기 + 증분 재계산 작업
      setPhase("upload");
      const r = await api.uploadChange(file, kind, documentId || undefined);
      setUpload(r);
      if (r.job_id) {
        setJobId(r.job_id);
        setPhase("running");
      } else {
        setPhase("idle");
      }
      setFile(null);
    } catch (err) {
      setError(errorText(err));
      setPhase("idle");
    }
  }

  const busy = phase === "snapshot" || phase === "upload" || (phase === "running" && !(job && TERMINAL.has(job.status)));

  return (
    <>
      <p>
        정정된 정산서, 새 입금 내역, 추가 약정서 등을 올리면 영향받는 결과만 다시 계산합니다. 올리기 직전의 결과를
        보관해 두었다가 이전/현재를 나란히 보여 줍니다.
      </p>
      {canWrite(session?.role) ? (
        <form className="card" onSubmit={submit}>
          <DropZone
            file={file}
            disabled={busy}
            onFile={(f, p) => {
              setFile(p ? null : f);
              setProblem(p);
            }}
          />
          {problem && (
            <Msg kind="failure" title="올릴 수 없는 파일">
              {problem}
            </Msg>
          )}
          <div className="grid2" style={{ marginTop: "0.5rem" }}>
            <div className="field">
              <label htmlFor="ckind">자료 종류</label>
              <KindSelect id="ckind" value={kind} onChange={setKind} />
            </div>
            <div className="field">
              <label htmlFor="ctarget">정정 대상 문서 (정정본일 때)</label>
              <select id="ctarget" value={documentId} onChange={(e) => setDocumentId(e.target.value)}>
                <option value="">없음 — 새 자료</option>
                {docs.data?.items.map((d) => (
                  <option key={d.document_id} value={d.document_id}>
                    {d.latest.filename} (현재 v{d.latest.version})
                  </option>
                ))}
              </select>
              <p className="hint">정정본으로 올리면 같은 문서의 새 버전이 되고, 없어진 행의 결과는 &lsquo;없어짐&rsquo;으로 표시됩니다.</p>
            </div>
          </div>
          {error && <Msg kind="failure">{error}</Msg>}
          <button type="submit" disabled={!file || Boolean(problem) || busy}>
            {phase === "snapshot" ? "이전 결과 보관 중…" : phase === "upload" ? "올리는 중…" : "올리고 영향 비교"}
          </button>
        </form>
      ) : (
        <Msg kind="info">보기 전용 역할은 자료를 올릴 수 없습니다.</Msg>
      )}

      {before && (
        <p className="hint">
          이전 결과 보관: {kst(before.at)} · 결과 {before.total}건 중 상세 {before.detailed}건
          {before.truncated ? " (결과가 많아 일부만 보관)" : ""}
        </p>
      )}

      {upload && upload.status === "unchanged" && (
        <Msg kind="info" title="바뀐 것 없음">
          같은 내용의 파일이 이미 저장되어 있습니다({upload.document.filename}). 다시 계산할 것이 없습니다.
        </Msg>
      )}

      {jobId && !job && <Loading text="작업 상태 확인 중" />}
      {job && <Outcome job={job} before={before} />}
    </>
  );
}

function outcomeOf(job: Job): ChangeOutcome | null {
  if (job.status !== "succeeded") return null;
  if (job.type === "apply_change") return job.result as ChangeOutcome;
  const r = job.result as IngestJobResult | null;
  return r?.impact ?? null;
}

function Outcome({ job, before }: { job: Job; before: Before | null }) {
  const outcome = outcomeOf(job);
  const ids = outcome
    ? [...new Set([...outcome.plan.affected, ...outcome.changed, ...outcome.review_required, ...outcome.removed])]
    : [];
  const after = useApi<Map<string, Snapshot>>(outcome ? `after:${job.id}` : null, async () => {
    const removed = new Set(outcome!.removed);
    const want = ids.filter((id) => !removed.has(id)).slice(0, AFTER_DETAIL_CAP);
    const details = await mapLimit(want, 6, (id) => api.decision(id));
    const m = new Map<string, Snapshot>();
    details.forEach((d) => {
      if (d) m.set(d.id, snapshotOf(d, won));
    });
    return m;
  });

  return (
    <div className="card">
      <div className="row">
        <b>읽기·재계산 작업</b> <JobBadge status={job.status} />
        <Link className="small" href={`/job?id=${encodeURIComponent(job.id)}`}>
          작업 상세
        </Link>
      </div>
      {!TERMINAL.has(job.status) && <Loading text="처리 중(자동으로 다시 확인)" />}
      {TERMINAL.has(job.status) && !outcome && <JobResult job={job} />}
      {outcome && (
        <>
          <ImpactSummary o={outcome} />
          {ids.length === 0 ? (
            <Msg kind="empty">영향받은 결과가 없습니다. 이 자료로 바뀐 판단은 없습니다.</Msg>
          ) : (
            <>
              <h3>이전 / 현재 비교</h3>
              {after.error ? <Msg kind="failure">{errorText(after.error)}</Msg> : null}
              {after.loading && <Loading text="현재 결과를 불러오는 중" />}
              {after.data && before && (
                <CompareTable
                  rows={compare(
                    ids,
                    before.snaps,
                    after.data,
                    new Set(outcome.review_required),
                    new Set(outcome.removed),
                  )}
                />
              )}
              {after.data && !before && <Msg kind="info">이전 결과를 보관하지 못해 현재 결과만 볼 수 있습니다.</Msg>}
              {ids.length > AFTER_DETAIL_CAP && (
                <p className="hint">영향받은 결과가 많아 처음 {AFTER_DETAIL_CAP}건만 비교했습니다.</p>
              )}
            </>
          )}
        </>
      )}
    </div>
  );
}

const CHANGE_LABEL: Record<CompareRow["change"], string> = {
  changed: "결과 바뀜",
  added: "새 결과",
  removed: "없어짐",
  review_required: "재확인 필요",
  unchanged: "같음",
};
const CHANGE_CLASS: Record<CompareRow["change"], string> = {
  changed: "diff-changed",
  added: "diff-added",
  removed: "diff-removed",
  review_required: "diff-changed",
  unchanged: "",
};

function Side({ s }: { s: Snapshot | null }) {
  if (!s) return <span className="muted">-</span>;
  return (
    <div className="small">
      <StatusBadge status={s.status} /> <ReviewBadge status={s.review_status} />
      <div>차액 {won(s.open)}</div>
      <div>기한 {s.due}</div>
      <div>지연이자 {s.interest}</div>
      {s.missing.length > 0 && <div className="hint">미확인: {s.missing.map((m) => label(MISSING_LABEL, m)).join(", ")}</div>}
    </div>
  );
}

function CompareTable({ rows }: { rows: CompareRow[] }) {
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>결과</th>
            <th>변화</th>
            <th>이전</th>
            <th>현재</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id} className={CHANGE_CLASS[r.change]}>
              <td className="small">
                {r.change === "removed" ? (
                  <span className="mono">{r.id}</span>
                ) : (
                  <Link className="mono" href={`/decision?id=${encodeURIComponent(r.id)}`}>
                    {r.id}
                  </Link>
                )}
              </td>
              <td className="nowrap">{CHANGE_LABEL[r.change]}</td>
              <td>
                <Side s={r.before} />
              </td>
              <td>
                <Side s={r.after} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
