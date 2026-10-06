import Link from "next/link";
import type { AnalysisJobResult, ChangeOutcome, IngestJobResult, Job } from "@/lib/api";
import { REVIEW_LABEL, STATUS_LABEL, docStatusMessage, label } from "@/lib/fmt";
import { DocBadge } from "./Badge";
import { DocApplication } from "./DocApplication";
import { Msg } from "./Msg";

/** 작업 결과를 종류별로 보여 준다. 실패·자료 없음·판단 보류를 구분한다. */
export function JobResult({ job }: { job: Job }) {
  if (job.status === "failed") {
    return (
      <Msg kind="failure">
        작업이 끝나지 못했습니다{job.error?.message ? `: ${job.error.message}` : ""}
        {job.error?.code ? <span className="mono"> ({job.error.code})</span> : null}. 결과가 없다는 뜻이 아니라 처리가
        실패한 것입니다.
      </Msg>
    );
  }
  if (job.status === "cancelled") return <Msg kind="info">작업이 취소되었습니다. 바뀐 내용은 없습니다.</Msg>;
  if (job.status !== "succeeded") return null;
  const r = job.result;
  if (job.type === "ingest_document") return <IngestResult r={r as IngestJobResult} />;
  if (job.type === "run_analysis") return <AnalysisResult r={r as AnalysisJobResult} />;
  if (job.type === "apply_change") return <ImpactSummary o={r as ChangeOutcome} />;
  return <pre className="src">{JSON.stringify(r, null, 2)}</pre>;
}

function IngestResult({ r }: { r: IngestJobResult }) {
  const m = docStatusMessage(r.document_status, r.reason);
  const app = r.application;
  return (
    <>
      <p>
        문서 상태(양식 인식): <DocBadge status={r.document_status} />
        <span className="hint"> · 문서 v{r.version}</span>
      </p>
      {r.document_status !== "PARSED" && (
        <Msg kind={m.kind} title={r.document_status === "NEEDS_MAPPING" ? "열 매핑 확인 필요" : undefined}>
          {m.text}
        </Msg>
      )}
      {r.document_status === "PARSED" && (
        <p className="hint">
          양식을 읽었습니다(거래 {r.records}건, 사실 {r.facts}건). 모든 거래를 반영했는지는 아래 반영 결과를
          보세요.
        </p>
      )}
      {app && (
        <DocApplication
          outcome={app}
          counts={r.counts}
          issues={r.issues ?? []}
          issuesTotal={r.issues_total}
          totals={r.totals ?? []}
          isCurrent={app.current_doc_version_id === r.doc_version_id}
        />
      )}
      {r.document_status === "NEEDS_MAPPING" && (
        <p>
          <Link className="btn" href={`/mapping?dv=${encodeURIComponent(r.doc_version_id)}`}>
            열 매핑 확인하기
          </Link>
        </p>
      )}
      {r.impact && <ImpactSummary o={r.impact} />}
    </>
  );
}

function AnalysisResult({ r }: { r: AnalysisJobResult }) {
  if (r.decisions === 0) {
    return (
      <Msg kind="empty">
        분석할 거래가 없습니다. 정산서·세금계산서 등 거래 문서를 먼저 올리세요.{" "}
        <Link href="/upload">문서 업로드</Link>
      </Msg>
    );
  }
  return (
    <>
      <Msg kind="ok">거래 {r.decisions}건을 분석했습니다.</Msg>
      <div className="grid2">
        <div>
          <h3>대사 상태</h3>
          <ul className="plain">
            {Object.entries(r.by_status).map(([k, n]) => (
              <li key={k}>
                <Link href={`/results?status=${k}`}>{label(STATUS_LABEL, k)}</Link>: {n}건
              </li>
            ))}
          </ul>
        </div>
        <div>
          <h3>확인 상태</h3>
          <ul className="plain">
            {Object.entries(r.by_review_status).map(([k, n]) => (
              <li key={k}>
                {label(REVIEW_LABEL, k)}: {n}건
              </li>
            ))}
          </ul>
        </div>
      </div>
      {r.unattributed_payments.length > 0 && (
        <Msg kind="pending" title="거래처 미확인 입금">
          거래처를 정하지 못한 입금 {r.unattributed_payments.length}건이 있어 어느 거래에도 연결하지 않았습니다.
        </Msg>
      )}
      <p className="hint mono">스냅숏 {r.snapshot_hash}</p>
      <p>
        <Link className="btn" href="/results">
          결과 목록 보기
        </Link>
      </p>
    </>
  );
}

export function ImpactSummary({ o }: { o: ChangeOutcome }) {
  return (
    <div className="card">
      <h3 style={{ marginTop: 0 }}>영향 범위</h3>
      <dl className="kv">
        <dt>재계산 방식</dt>
        <dd>{o.plan.fallback_full ? "전체 재계산" : "영향받은 거래만 재계산"}</dd>
        <dt>설명</dt>
        <dd>{o.plan.summary || "-"}</dd>
        <dt>영향받은 결과</dt>
        <dd>{o.plan.affected.length}건</dd>
        <dt>결과가 바뀜</dt>
        <dd>{o.changed.length}건</dd>
        <dt>재확인 필요</dt>
        <dd>{o.review_required.length}건</dd>
        <dt>없어진 결과</dt>
        <dd>{o.removed.length}건</dd>
        <dt>다시 계산한 거래처</dt>
        <dd>{o.recomputed_groups.join(", ") || "-"}</dd>
      </dl>
      {o.plan.notes.length > 0 && (
        <ul className="plain hint">
          {o.plan.notes.map((n, i) => (
            <li key={i}>{n}</li>
          ))}
        </ul>
      )}
      {o.review_required.length > 0 && (
        <Msg kind="pending" title="재확인 필요">
          이미 확인(승인)한 결과 중 근거가 바뀐 것이 있습니다. 과거 승인 기록은 남아 있으며, 현재 결과를 다시 확인해야
          합니다.
        </Msg>
      )}
    </div>
  );
}
