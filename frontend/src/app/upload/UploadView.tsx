"use client";

import Link from "next/link";
import { useState, type FormEvent } from "react";
import { DocBadge } from "@/components/Badge";
import { ApplyBadge } from "@/components/DocApplication";
import { DropZone } from "@/components/DropZone";
import { KindSelect } from "@/components/KindSelect";
import { Loading, Msg } from "@/components/Msg";
import { useApi } from "@/components/useApi";
import { useSession } from "@/components/useSession";
import { api, type DocKind, type DocumentPage, type UploadResponse } from "@/lib/api";
import { DOC_KIND_LABEL, bytes, docStatusMessage, errorText, kst, label, shortHash } from "@/lib/fmt";
import { canWrite } from "@/lib/session";

export default function UploadView() {
  const session = useSession();
  const [file, setFile] = useState<File | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [kind, setKind] = useState<DocKind>("other");
  const [documentId, setDocumentId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<UploadResponse | null>(null);

  const docs = useApi<DocumentPage>("docs", () => api.listDocuments(null, 100));
  const [more, setMore] = useState<DocumentPage | null>(null);
  const [moreError, setMoreError] = useState<string | null>(null);

  async function loadMore(cursor: string) {
    try {
      const page = await api.listDocuments(cursor, 100);
      setMore((prev) => ({ items: [...(prev?.items ?? []), ...page.items], next_cursor: page.next_cursor }));
      setMoreError(null);
    } catch (e) {
      setMoreError(errorText(e));
    }
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!file || problem) return;
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const r = await api.uploadDocument(file, kind, documentId || undefined);
      setResult(r);
      setFile(null);
      setMore(null);
      docs.reload();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  const writable = canWrite(session?.role);
  const items = [...(docs.data?.items ?? []), ...(more?.items ?? [])];
  const nextCursor = more ? more.next_cursor : docs.data?.next_cursor;

  return (
    <>
      {!writable && (
        <Msg kind="info">보기 전용 역할(viewer)은 업로드할 수 없습니다. 문서 목록만 볼 수 있습니다.</Msg>
      )}
      {writable && (
        <form className="card" onSubmit={submit}>
          <DropZone
            file={file}
            disabled={busy}
            onFile={(f, p) => {
              setFile(p ? null : f);
              setProblem(p);
              setResult(null);
            }}
          />
          {problem && (
            <Msg kind="failure" title="올릴 수 없는 파일">
              {problem}
            </Msg>
          )}
          <div className="grid2" style={{ marginTop: "0.5rem" }}>
            <div className="field">
              <label htmlFor="kind">문서 종류</label>
              <KindSelect id="kind" value={kind} onChange={setKind} />
              <p className="hint">모르면 &lsquo;기타(자동 판별)&rsquo;를 고르세요. 열 이름으로 양식을 판별합니다.</p>
            </div>
            <div className="field">
              <label htmlFor="docid">기존 문서의 새 버전으로 올리기 (선택)</label>
              <select id="docid" value={documentId} onChange={(e) => setDocumentId(e.target.value)}>
                <option value="">새 문서</option>
                {items.map((d) => (
                  <option key={d.document_id} value={d.document_id}>
                    {d.latest.filename} (v{d.latest.version})
                  </option>
                ))}
              </select>
              <p className="hint">
                정정본은 <Link href="/changes">정정·추가 자료</Link> 화면에서 올리면 영향받은 결과의 이전/현재 비교까지
                볼 수 있습니다.
              </p>
            </div>
          </div>
          {error && <Msg kind="failure">{error}</Msg>}
          <button type="submit" disabled={!file || Boolean(problem) || busy}>
            {busy ? "올리는 중…" : "올리기"}
          </button>
        </form>
      )}

      {result && <UploadResult r={result} />}

      <h2>올린 문서</h2>
      {docs.error ? <Msg kind="failure">{errorText(docs.error)}</Msg> : null}
      {docs.loading && !docs.data && <Loading />}
      {docs.data && items.length === 0 && <Msg kind="empty">아직 올린 문서가 없습니다.</Msg>}
      {items.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>파일</th>
                <th>종류</th>
                <th>버전</th>
                <th>상태</th>
                <th className="num">크기</th>
                <th>올린 시각</th>
                <th>할 일</th>
              </tr>
            </thead>
            <tbody>
              {items.map(({ document_id, latest: d }) => {
                const m = docStatusMessage(d.status, d.status_detail?.reason as string | null | undefined);
                return (
                  <tr key={document_id}>
                    <td>
                      {d.filename}
                      <div className="hint mono" title={d.content_hash}>
                        {d.id} · sha256 {shortHash(d.content_hash)}
                      </div>
                    </td>
                    <td>{label(DOC_KIND_LABEL, d.kind)}</td>
                    <td>
                      v{d.version}
                      {!d.is_current && d.current_version !== null && (
                        <div className="hint">장부 반영: v{d.current_version}</div>
                      )}
                    </td>
                    <td>
                      <DocBadge status={d.status} /> <ApplyBadge state={d.apply_state} />
                      {d.issue_count ? <div className="hint">행별 문제 {d.issue_count}건</div> : null}
                      {m.kind === "failure" || m.kind === "pending" ? <div className="hint">{m.text}</div> : null}
                    </td>
                    <td className="num">{bytes(d.size)}</td>
                    <td className="nowrap">{kst(d.created_at)}</td>
                    <td>
                      <Link href={`/mapping?dv=${encodeURIComponent(d.id)}`}>
                        {d.status === "NEEDS_MAPPING" ? "열 매핑 확인" : "열 매핑 보기"}
                      </Link>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {moreError && <Msg kind="failure">{moreError}</Msg>}
      {nextCursor && (
        <p>
          <button className="secondary" onClick={() => loadMore(nextCursor)}>
            더 보기
          </button>
        </p>
      )}
    </>
  );
}

function UploadResult({ r }: { r: UploadResponse }) {
  const d = r.document;
  return (
    <div className="card">
      {r.duplicate ? (
        <Msg kind="info" title="이미 있는 파일">
          같은 내용의 파일이 이미 저장되어 있어 새로 저장하지 않았습니다 ({d.filename}, v{d.version}).
        </Msg>
      ) : (
        <Msg kind="ok" title="저장함">
          {d.filename} (v{d.version})을(를) 저장했습니다. 읽기 작업이 끝나면 상태가 바뀝니다.
        </Msg>
      )}
      <dl className="kv">
        <dt>문서 버전 ID</dt>
        <dd className="mono">{d.id}</dd>
        <dt>상태</dt>
        <dd>
          <DocBadge status={d.status} />
        </dd>
      </dl>
      <p className="row">
        {r.job_id && (
          <Link className="btn" href={`/job?id=${encodeURIComponent(r.job_id)}`}>
            읽기 작업 상태 보기
          </Link>
        )}
        <Link className="btn secondary" href={`/mapping?dv=${encodeURIComponent(d.id)}`}>
          열 매핑 보기
        </Link>
      </p>
    </div>
  );
}
