"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useMemo, useState, type FormEvent } from "react";
import { DocBadge } from "@/components/Badge";
import { ApplyBadge, DocApplication } from "@/components/DocApplication";
import { Loading, Msg } from "@/components/Msg";
import { useApi } from "@/components/useApi";
import { useSession } from "@/components/useSession";
import { api, type DocumentVersion, type MappingOut } from "@/lib/api";
import { parseCsvPreview } from "@/lib/csv";
import {
  CONFIDENCE_LABEL,
  DOC_KIND_LABEL,
  decodeText,
  docStatusMessage,
  errorText,
  kst,
  label,
} from "@/lib/fmt";
import {
  buildMapping,
  duplicateColumns,
  initialSelection,
  missingRequired,
  type OptionValues,
  type Selection,
} from "@/lib/mapping";
import { canWrite } from "@/lib/session";

const FORMAT_LABEL: Record<string, string> = {
  hometax_etax_list: "홈택스 전자세금계산서 목록",
  kr_bank_txn: "은행 거래내역",
  retail_settlement: "유통사 정산서",
  agreement_terms: "약정 조건표",
};

export default function MappingView() {
  const dv = useSearchParams().get("dv");
  const data = useApi<{ doc: DocumentVersion; mapping: MappingOut }>(dv ? `map:${dv}` : null, async () => {
    const [doc, mapping] = await Promise.all([api.version(dv!), api.getMapping(dv!)]);
    return { doc, mapping };
  });

  if (!dv) {
    return (
      <Msg kind="info">
        문서를 먼저 고르세요. <Link href="/upload">문서 업로드</Link> 화면의 목록에서 &lsquo;열 매핑&rsquo;을 누르면 됩니다.
      </Msg>
    );
  }
  if (data.error) return <Msg kind="failure">{errorText(data.error)}</Msg>;
  if (!data.data) return <Loading />;
  const { doc, mapping } = data.data;
  // key: 다른 문서·확정 시점이면 폼 상태를 새로 시작
  return <MappingForm key={`${dv}:${mapping.confirmed?.confirmed_at ?? "-"}`} doc={doc} m={mapping} />;
}

function MappingForm({ doc, m }: { doc: DocumentVersion; m: MappingOut }) {
  const router = useRouter();
  const session = useSession();
  const init = useMemo(() => initialSelection(m), [m]);
  const [sel, setSel] = useState<Selection>(init.selection);
  const [opts, setOpts] = useState<OptionValues>(init.options);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [preview, setPreview] = useState<{ rows: string[][]; encoding: string } | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);

  const s = m.suggestion;
  const headers = s?.headers ?? [];
  const fields = s?.fields ?? [];
  const matchBy = new Map((s?.matches ?? []).map((x) => [x.field, x]));
  const dup = duplicateColumns(sel);
  const missing = missingRequired(m, sel);
  const status = docStatusMessage(m.document_status, doc.status_detail?.reason as string | null | undefined);
  const isText = /\.(csv|txt)$/i.test(doc.filename) || doc.media_type.startsWith("text/");
  const writable = canWrite(session?.role);

  async function loadPreview() {
    setPreviewError(null);
    try {
      const buf = await api.content(doc.id);
      const t = decodeText(buf);
      if (!t) {
        setPreviewError("파일 인코딩을 알아보지 못해 미리보기를 만들지 못했습니다.");
        return;
      }
      setPreview({ rows: parseCsvPreview(t.text, 6), encoding: t.encoding });
    } catch (e) {
      setPreviewError(errorText(e));
    }
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const r = await api.confirmMapping(doc.id, buildMapping(sel, init.selection, opts, init.formatId, init.table), true);
      if (r.job_id) router.push(`/job?id=${encodeURIComponent(r.job_id)}`);
      else router.push("/upload");
    } catch (err) {
      setError(errorText(err));
      setBusy(false);
    }
  }

  return (
    <>
      <div className="card">
        <dl className="kv">
          <dt>파일</dt>
          <dd>
            {doc.filename} (v{doc.version}, {label(DOC_KIND_LABEL, doc.kind)})
          </dd>
          <dt>문서 상태</dt>
          <dd>
            <DocBadge status={m.document_status} /> <ApplyBadge state={doc.apply_state} />
          </dd>
          <dt>현재 반영 버전</dt>
          <dd>
            {doc.current_version ? `v${doc.current_version}` : "없음"}
            {doc.is_current ? " (이 버전)" : ""}
          </dd>
          <dt>인식한 양식</dt>
          <dd>
            {s?.format_id ? label(FORMAT_LABEL, s.format_id) : "-"}
            {s?.overall ? ` · 전체 신뢰도 ${label(CONFIDENCE_LABEL, s.overall)}` : ""}
            {s?.sources?.length ? <span className="hint"> (제안 근거: {s.sources.join(", ")})</span> : null}
          </dd>
          {m.confirmed && (
            <>
              <dt>마지막 확정</dt>
              <dd>{kst(m.confirmed.confirmed_at)}</dd>
            </>
          )}
        </dl>
        {status.kind !== "ok" && <Msg kind={status.kind}>{status.text}</Msg>}
        {!doc.is_current && doc.current_version !== null && doc.current_version > doc.version && (
          <Msg kind="info" title="이전 버전">
            이 문서는 v{doc.version}이고 현재 장부에는 v{doc.current_version}이 반영되어 있습니다. 이 버전의 매핑을
            확정해 다시 읽어도 현재 장부에는 반영되지 않습니다.
          </Msg>
        )}
        {m.confirmed?.legacy_unreadable && (
          <Msg kind="pending" title="예전 형식의 매핑">
            이전에 확정한 매핑이 예전 형식(열 이름)으로 저장되어 있어 그대로 쓸 수 없습니다. 열을 다시 골라 확정하세요.
          </Msg>
        )}
        {(doc.status_detail?.notes as string[] | undefined)?.length ? (
          <ul className="plain hint">
            {(doc.status_detail?.notes as string[]).map((n, i) => (
              <li key={i}>{n}</li>
            ))}
          </ul>
        ) : null}
        <p className="hint">
          신뢰도는 열 이름·값 형식이 알려진 이름과 얼마나 맞는지에 따른 구간(확인됨/높음/중간/낮음/없음)이며 확률이
          아닙니다.
        </p>
      </div>

      {doc.status_detail?.application && (
        <DocApplication
          outcome={doc.status_detail.application}
          counts={doc.status_detail.counts}
          issues={doc.status_detail.issues ?? []}
          issuesTotal={doc.status_detail.issues_total}
          totals={doc.status_detail.totals ?? []}
          isCurrent={doc.is_current}
        />
      )}

      {!s || fields.length === 0 ? (
        <Msg kind="empty">
          이 문서에서 매핑을 제안할 표를 찾지 못했습니다. 표의 첫 행에 열 이름이 있는지 확인하고 다시 올려 주세요.
        </Msg>
      ) : (
        <form onSubmit={submit}>
          {isText && (
            <div className="card">
              <div className="row">
                <b>원본 미리보기</b>
                <button type="button" className="secondary" onClick={loadPreview}>
                  처음 몇 행 보기
                </button>
              </div>
              {previewError && <Msg kind="failure">{previewError}</Msg>}
              {preview && (
                <div className="table-wrap" style={{ marginTop: "0.5rem" }}>
                  <table>
                    <tbody>
                      {preview.rows.map((r, i) => (
                        <tr key={i}>
                          <th className="muted">{i + 1}행</th>
                          {r.map((c, j) => (
                            <td key={j}>{c}</td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  <p className="hint" style={{ padding: "0 0.6rem" }}>
                    인코딩 {preview.encoding}
                  </p>
                </div>
              )}
            </div>
          )}

          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>항목</th>
                  <th>원본 열</th>
                  <th>제안 신뢰도</th>
                  <th>제안 이유</th>
                </tr>
              </thead>
              <tbody>
                {fields.map((f) => {
                  const match = matchBy.get(f.name);
                  const v = sel[f.name];
                  return (
                    <tr key={f.name}>
                      <td>
                        <label htmlFor={`f-${f.name}`} style={{ margin: 0 }}>
                          {f.label}
                          {f.required && <span className="badge b-warn" style={{ marginLeft: 6 }}>필수</span>}
                        </label>
                        <div className="hint mono">
                          {f.name} · {f.kind}
                        </div>
                      </td>
                      <td>
                        <select
                          id={`f-${f.name}`}
                          value={v === null || v === undefined ? "" : String(v)}
                          disabled={!writable}
                          onChange={(e) =>
                            setSel({ ...sel, [f.name]: e.target.value === "" ? null : Number(e.target.value) })
                          }
                        >
                          <option value="">(매핑 안 함)</option>
                          {headers.map((h, i) => (
                            <option key={i} value={i}>
                              {i + 1}. {h || "(이름 없음)"}
                            </option>
                          ))}
                        </select>
                        {v !== null && v !== undefined && dup.includes(v) && (
                          <div className="hint" style={{ color: "var(--bad)" }}>
                            같은 열을 여러 항목에 골랐습니다.
                          </div>
                        )}
                      </td>
                      <td>
                        {match ? (
                          <span className="badge b-neutral">{label(CONFIDENCE_LABEL, match.confidence)}</span>
                        ) : (
                          <span className="muted">-</span>
                        )}
                      </td>
                      <td className="small muted">{match?.reason ?? ""}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>

          <div className="card">
            <h3 style={{ marginTop: 0 }}>추가 정보(선택)</h3>
            <div className="grid2">
              <div className="field">
                <label htmlFor="o-cp">거래처(유통사) 이름 직접 입력</label>
                <input
                  id="o-cp"
                  value={opts.counterparty_override ?? ""}
                  disabled={!writable}
                  onChange={(e) => setOpts({ ...opts, counterparty_override: e.target.value })}
                />
                <p className="hint">
                  입력하면 모든 행의 거래처로 쓰며, 위에서 고른 거래처 열보다 우선합니다. 거래처 열이 있으면 비워 두세요.
                </p>
              </div>
              {s.format_id === "kr_bank_txn" && (
                <div className="field">
                  <label htmlFor="o-acct">계좌 이름 직접 입력</label>
                  <input
                    id="o-acct"
                    value={opts.account_override ?? ""}
                    disabled={!writable}
                    onChange={(e) => setOpts({ ...opts, account_override: e.target.value })}
                  />
                  <p className="hint">입력하면 문서 머리말의 계좌 정보보다 우선합니다.</p>
                </div>
              )}
              {s.format_id === "hometax_etax_list" && (
                <>
                  <div className="field">
                    <label htmlFor="o-dir">매출·매입 구분</label>
                    <select
                      id="o-dir"
                      value={opts.direction ?? ""}
                      disabled={!writable}
                      onChange={(e) => setOpts({ ...opts, direction: e.target.value })}
                    >
                      <option value="">자동(사업자번호로 판별)</option>
                      <option value="sales">매출(우리가 공급자)</option>
                      <option value="purchase">매입(우리가 공급받는 자)</option>
                    </select>
                  </div>
                  <div className="field">
                    <label htmlFor="o-brn">우리 회사 사업자등록번호</label>
                    <input
                      id="o-brn"
                      inputMode="numeric"
                      value={opts.self_brn ?? ""}
                      disabled={!writable}
                      onChange={(e) => setOpts({ ...opts, self_brn: e.target.value })}
                    />
                  </div>
                </>
              )}
            </div>
          </div>

          {missing.length > 0 && (
            <Msg kind="pending" title="필수 항목 미지정">
              {missing.map((n) => fields.find((f) => f.name === n)?.label ?? n).join(", ")} 열을 고르지 않으면 해당
              행을 읽지 못할 수 있습니다.
            </Msg>
          )}
          {error && <Msg kind="failure">{error}</Msg>}
          {writable ? (
            <button type="submit" disabled={busy || dup.length > 0}>
              {busy ? "확정 중…" : "매핑 확정하고 다시 읽기"}
            </button>
          ) : (
            <Msg kind="info">보기 전용 역할은 매핑을 확정할 수 없습니다.</Msg>
          )}
          <p className="hint">확정한 매핑은 기록으로 남고, 문서를 다시 읽는 작업이 만들어집니다.</p>
        </form>
      )}
    </>
  );
}
