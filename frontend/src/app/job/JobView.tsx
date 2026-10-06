"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useState } from "react";
import { JobBadge } from "@/components/Badge";
import { JobResult } from "@/components/JobResult";
import { Loading, Msg } from "@/components/Msg";
import { TERMINAL, useJobPoll } from "@/components/useJobPoll";
import { useSession } from "@/components/useSession";
import { api } from "@/lib/api";
import { JOB_TYPE_LABEL, errorText, kst, label } from "@/lib/fmt";
import { canWrite } from "@/lib/session";

export default function JobView() {
  const id = useSearchParams().get("id");
  const { job, error, polls, refresh } = useJobPoll(id);
  const session = useSession();
  const [cancelError, setCancelError] = useState<string | null>(null);

  if (!id) {
    return (
      <Msg kind="info">
        작업 ID가 없습니다. <Link href="/analysis">분석</Link> 화면에서 최근 작업을 고르세요.
      </Msg>
    );
  }
  if (!job && error) return <Msg kind="failure">{errorText(error)}</Msg>;
  if (!job) return <Loading text="작업 상태 확인 중" />;

  const done = TERMINAL.has(job.status);
  return (
    <>
      <div className="card">
        <div className="row">
          <b>{label(JOB_TYPE_LABEL, job.type)}</b> <JobBadge status={job.status} />
          {!done && (
            <span className="muted small">
              <span className="spin" aria-hidden /> 자동으로 다시 확인하는 중 ({polls}회)
            </span>
          )}
        </div>
        <dl className="kv small" style={{ marginTop: "0.5rem" }}>
          <dt>작업 ID</dt>
          <dd className="mono">{job.id}</dd>
          <dt>만든 시각</dt>
          <dd>{kst(job.created_at)}</dd>
          <dt>시작</dt>
          <dd>{kst(job.started_at)}</dd>
          <dt>끝</dt>
          <dd>{kst(job.finished_at)}</dd>
          <dt>시도</dt>
          <dd>
            {job.attempts} / {job.max_attempts}
            {job.status === "queued" && job.attempts > 0 ? " (일시적 오류로 다시 시도 대기)" : ""}
          </dd>
          {job.type === "run_analysis" && (
            <>
              <dt>설정</dt>
              <dd className="mono">{JSON.stringify(job.payload)}</dd>
            </>
          )}
        </dl>
        {job.status === "queued" && polls > 10 && (
          <Msg kind="info">
            오래 대기 중입니다. 작업 처리기(worker)가 실행 중인지 확인하세요: <code>jettae worker run</code>
          </Msg>
        )}
        {error ? <Msg kind="failure">상태 확인 중 오류: {errorText(error)} (계속 다시 시도합니다)</Msg> : null}
        {!done && canWrite(session?.role) && (
          <button
            className="secondary"
            disabled={job.cancel_requested}
            onClick={async () => {
              setCancelError(null);
              try {
                refresh(await api.cancelJob(job.id));
              } catch (e) {
                setCancelError(errorText(e));
              }
            }}
          >
            {job.cancel_requested ? "취소 요청됨" : "작업 취소"}
          </button>
        )}
        {cancelError && <Msg kind="failure">{cancelError}</Msg>}
      </div>
      <JobResult job={job} />
    </>
  );
}
