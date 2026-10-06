"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useState } from "react";
import { ReviewBadge, StatusBadge } from "@/components/Badge";
import { DueTable } from "@/components/DueView";
import { Investigations } from "@/components/Investigations";
import { Loading, Msg } from "@/components/Msg";
import { SourcePreview } from "@/components/SourcePreview";
import { useApi } from "@/components/useApi";
import { useSession } from "@/components/useSession";
import { ApiError, api, mapLimit, type DecisionDetail, type DocumentVersion } from "@/lib/api";
import {
  MISSING_LABEL,
  STATUS_HELP,
  UNRESOLVED_LABEL,
  errorText,
  isPendingStatus,
  kst,
  label,
  shortHash,
  type MsgKind,
  showValue,
  won,
} from "@/lib/fmt";
import { approveErrorView, type ApproveErrorView } from "@/lib/apply";
import { canWrite } from "@/lib/session";
import {
  DOC_ENTITY_LABEL,
  EVIDENCE_ROLE_LABEL,
  LINK_METHOD_LABEL,
  RULE_INFO,
  contractInfo,
  evidenceInfo,
  factsByDoc,
  pendingText,
  keyNumbers,
  splitRuleVersion,
  subjectLabel,
  type ContractInfo,
  type EvidenceInfo,
} from "@/lib/view";

interface Loaded {
  d: DecisionDetail;
  docs: Map<string, DocumentVersion | null>;
  loadedAt: string;
}

export default function DecisionView() {
  const id = useSearchParams().get("id");
  const data = useApi<Loaded>(id ? `dec:${id}` : null, async () => {
    const d = await api.decision(id!);
    const dvs = [...new Set(d.facts.map((f) => f.span?.doc_version_id).filter((x): x is string => Boolean(x)))];
    const docs = await mapLimit(dvs, 4, (dv) => api.version(dv));
    return { d, docs: new Map(dvs.map((dv, i) => [dv, docs[i]])), loadedAt: new Date().toISOString() };
  });

  if (!id) {
    return (
      <Msg kind="info">
        결과를 고르세요. <Link href="/results">결과 목록</Link>
      </Msg>
    );
  }
  if (data.error && !data.data) return <Msg kind="failure">{errorText(data.error)}</Msg>;
  if (!data.data) return <Loading />;
  return <Detail loaded={data.data} reload={data.reload} reloading={data.loading} />;
}

function Detail({ loaded, reload, reloading }: { loaded: Loaded; reload: () => void; reloading: boolean }) {
  const { d, docs } = loaded;
  const k = keyNumbers(d);
  const groups = factsByDoc(d.facts);

  return (
    <>
      <div className="card">
        <h2 style={{ marginTop: 0 }}>{subjectLabel(d)}</h2>
        <div className="row">
          <StatusBadge status={d.status} />
          <ReviewBadge status={d.review_status} />
        </div>
        <p className="small">{STATUS_HELP[d.status]}</p>
        <dl className="kv small">
          <dt>거래 ID</dt>
          <dd className="mono">{d.subject_id}</dd>
          <dt>결과 해시</dt>
          <dd className="mono" title={d.result_hash}>
            {d.result_hash}
          </dd>
          <dt>입력 스냅숏</dt>
          <dd className="mono" title={d.snapshot_hash}>
            {shortHash(d.snapshot_hash, 16)}
          </dd>
          <dt>계산 기준일(as_of)</dt>
          <dd>{k.due.asOf ?? "-"}</dd>
          <dt>적용 규칙 버전</dt>
          <dd className="mono">{d.rule_versions.join(", ") || "-"}</dd>
          <dt>불러온 시각</dt>
          <dd>{kst(loaded.loadedAt)}</dd>
        </dl>
        {d.review_status === "REVIEW_REQUIRED" && (
          <Msg kind="pending" title="재확인 필요">
            확인(승인)한 뒤 관련 근거가 바뀌었습니다. 과거 승인 기록은 아래에 남아 있습니다. 현재 결과를 다시 확인하세요.
          </Msg>
        )}
      </div>

      <Approve d={d} reload={reload} reloading={reloading} />

      <Evidence d={d} reload={reload} reloading={reloading} />

      <h2>금액 차이</h2>
      <div className="card">
        <dl className="kv">
          <dt>정산(청구) 금액</dt>
          <dd>{won(k.amount)}</dd>
          <dt>연결된 입금 배분</dt>
          <dd>{won(k.allocated)}</dd>
          <dt>차액(미결)</dt>
          <dd>
            <b>{won(k.open)}</b>
          </dd>
          {k.feeDifference && k.feeDifference.amount !== 0 && (
            <>
              <dt>허용 오차로 처리한 차액</dt>
              <dd>{won(k.feeDifference)}</dd>
            </>
          )}
        </dl>
        {d.allocations.length > 0 ? (
          <>
            <h3>입금 배분</h3>
            <ul className="plain small">
              {d.allocations.map((a, i) => (
                <li key={i}>
                  <span className="mono">{String(a.source_id ?? "-")}</span> → {showValue(a.amount)}
                </li>
              ))}
            </ul>
          </>
        ) : (
          <p className="muted small">이 거래에 배분된 입금이 없습니다.</p>
        )}
        {k.reconReasons.length > 0 && (
          <ul className="plain small">
            {k.reconReasons.map((r, i) => (
              <li key={i}>{r}</li>
            ))}
          </ul>
        )}
        {k.candidates.length > 0 && (
          <>
            <h3>입금 후보(특정하지 않음)</h3>
            <pre className="src">{JSON.stringify(k.candidates, null, 2)}</pre>
          </>
        )}
      </div>

      <h2>지급기한 · 지연일수 · 지연이자</h2>
      <div className="card">
        {k.due.state === "insufficient" && (
          <Msg kind="pending" title="계산 보류">
            기준일 등 근거가 없어 계산하지 않았습니다. 세금계산서 작성일 등 다른 날짜로 대신하지 않습니다.
            {k.due.notes.length ? ` (${k.due.notes.join(" / ")})` : ""}
          </Msg>
        )}
        <DueTable due={k.due} />
        <Contract info={contractInfo(d)} />
      </div>

      <h2>계산 가정 · 확인이 필요한 조건 · 필요 서류</h2>
      <div className="grid2">
        <div className="card">
          <h3 style={{ marginTop: 0 }}>계산 가정</h3>
          {d.assumptions.length ? (
            <ul className="plain small">
              {d.assumptions.map((a, i) => (
                <li key={i}>{a}</li>
              ))}
            </ul>
          ) : (
            <p className="muted">-</p>
          )}
        </div>
        <div className="card">
          <h3 style={{ marginTop: 0 }}>확인이 필요한 조건</h3>
          {d.unresolved.length ? (
            <ul className="plain small">
              {d.unresolved.map((u) => (
                <li key={u}>{label(UNRESOLVED_LABEL, u)}</li>
              ))}
            </ul>
          ) : (
            <p className="muted">없음</p>
          )}
          {d.missing.length > 0 && (
            <p className="small">미확인 항목: {d.missing.map((m) => label(MISSING_LABEL, m)).join(", ")}</p>
          )}
        </div>
        <div className="card">
          <h3 style={{ marginTop: 0 }}>필요 서류</h3>
          {d.required_documents.length ? (
            <>
              <ul className="plain small">
                {d.required_documents.map((r, i) => (
                  <li key={i}>{r}</li>
                ))}
              </ul>
              <Link href="/changes" className="small">
                추가 자료 올리기
              </Link>
            </>
          ) : (
            <p className="muted">없음</p>
          )}
        </div>
      </div>
      {isPendingStatus(d.status) && d.required_documents.length === 0 && d.unresolved.length === 0 && (
        <Msg kind="pending">판단을 보류한 결과입니다. 아래 사실과 설명을 확인하세요.</Msg>
      )}

      <h2>Agent 조사</h2>
      <Investigations decisionId={d.id} />

      <h2>사용한 규칙과 출처</h2>
      <div className="card">
        {d.rule_versions.length === 0 ? (
          <p className="muted">적용한 규칙이 없습니다(계산 보류 또는 대사만 수행).</p>
        ) : (
          <ul className="plain">
            {d.rule_versions.map((rv) => {
              const { id, version } = splitRuleVersion(rv);
              const info = RULE_INFO[id];
              return (
                <li key={rv}>
                  {info ? info.title : id} <span className="mono small">({version})</span>
                  {info && (
                    <>
                      {" "}
                      —{" "}
                      <a href={info.url} target="_blank" rel="noopener noreferrer">
                        원문
                      </a>
                    </>
                  )}
                </li>
              );
            })}
          </ul>
        )}
        {k.due.sourceUrls.length > 0 && (
          <>
            <h3>계산에 기록된 출처</h3>
            <ul className="plain small">
              {k.due.sourceUrls.map((u) => (
                <li key={u}>
                  <a href={u} target="_blank" rel="noopener noreferrer">
                    {u}
                  </a>
                </li>
              ))}
            </ul>
          </>
        )}
      </div>

      <h2>사실과 원문 위치</h2>
      {d.facts.length === 0 && <Msg kind="empty">이 결과에 연결된 사실이 없습니다.</Msg>}
      {[...groups.entries()].map(([dv, facts]) =>
        dv ? (
          <SourcePreview key={dv} dv={dv} doc={docs.get(dv) ?? null} facts={facts} />
        ) : (
          <div className="card" key="nospan">
            <b>원문 위치가 없는 사실(계산·추론 값)</b>
            <ul className="plain small">
              {facts.map((f) => (
                <li key={f.id}>
                  {f.kind}: {showValue(f.value)} <span className="hint">({f.extractor})</span>
                </li>
              ))}
            </ul>
          </div>
        ),
      )}

      <h2>설명(엔진 값으로 만든 문장)</h2>
      <div className="src">{d.explanation}</div>

      <h2>기계적 검사</h2>
      <div className="card">
        <ul className="plain small">
          {d.checks.map((c, i) => (
            <li key={i}>
              <span className={`badge ${c.passed ? "b-ok" : "b-bad"}`}>{c.passed ? "통과" : "불일치"}</span> {c.name}
              {c.details.length > 0 && `: ${c.details.join("; ")}`}
            </li>
          ))}
        </ul>
        <p className="hint">인용 위치 존재, 설명 속 숫자와 계산 값 일치, 배분 합계 보존, 판단어 미사용 검사입니다.</p>
      </div>

      <h2>변경 이력</h2>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>기록 시각</th>
              <th>상태</th>
              <th>결과 해시</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {d.history.map((h, i) => (
              <tr key={i}>
                <td className="nowrap">{kst(h.recorded_at)}</td>
                <td>
                  <StatusBadge status={h.status} />
                </td>
                <td className="mono small">{shortHash(h.result_hash, 16)}</td>
                <td className="small">
                  {h.result_hash === d.result_hash ? "현재" : h.superseded ? "대체됨" : "이전"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

function Approve({ d, reload, reloading }: { d: DecisionDetail; reload: () => void; reloading: boolean }) {
  const session = useSession();
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{
    kind: MsgKind;
    title?: string;
    text: string;
    documents?: ApproveErrorView["documents"];
  } | null>(null);
  const current = d.approvals.some((a) => a.current);

  async function approve() {
    setBusy(true);
    setMsg(null);
    try {
      await api.approve(d.id, d.result_hash);
      setMsg({ kind: "ok", text: "현재 결과를 확인(승인)했습니다." });
      reload();
    } catch (e) {
      // 409 is either a changed result (stale_result) or an unacknowledged source document
      // (document_ack_required); see approveErrorView in lib/apply.ts.
      const v = approveErrorView(e, d.result_hash);
      setMsg({ kind: v.msgKind, title: v.title, text: v.text, documents: v.documents });
      if (v.reload) reload();
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card">
      <h3 style={{ marginTop: 0 }}>확인(승인)</h3>
      <p className="small">
        지금 보이는 결과(결과 해시 <span className="mono">{shortHash(d.result_hash)}</span>)를 검토했다는 기록을
        남깁니다. 근거가 바뀌면 자동으로 &lsquo;재확인 필요&rsquo;가 되며 과거 기록은 지워지지 않습니다. 법적 판단이
        아닙니다.
      </p>
      {msg && (
        <Msg kind={msg.kind} title={msg.title}>
          {msg.text}
          {msg.documents && msg.documents.length > 0 && (
            <ul className="plain small">
              {msg.documents.map((doc) => (
                <li key={doc.docVersionId}>
                  <Link href={doc.href}>
                    문서 {doc.documentId}
                    {doc.version !== null ? ` v${doc.version}` : ""} — 제외 행 확인하기
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </Msg>
      )}
      {canWrite(session?.role) ? (
        <button onClick={approve} disabled={busy || reloading || current}>
          {current ? "현재 결과 확인됨" : busy ? "처리 중…" : "이 결과 확인(승인)"}
        </button>
      ) : (
        <p className="muted small">보기 전용 역할은 확인할 수 없습니다.</p>
      )}
      {d.approvals.length > 0 && (
        <>
          <h3>확인 기록</h3>
          <ul className="plain small">
            {d.approvals.map((a) => (
              <li key={a.id}>
                {kst(a.approved_at)} · {a.approved_by} · 결과 <span className="mono">{shortHash(a.result_hash)}</span>{" "}
                {a.current ? (
                  <span className="badge b-ok">현재 결과</span>
                ) : (
                  <span className="badge b-neutral">이전 결과에 대한 확인</span>
                )}
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}

function docName(entity: string, id: string): string {
  return `${DOC_ENTITY_LABEL[entity] ?? entity} ${id}`;
}

/** 금액 기준 문서와 보강 증빙. 확인 대기 문서는 사용자가 같은 거래인지 답한다. */
function Evidence({ d, reload, reloading }: { d: DecisionDetail; reload: () => void; reloading: boolean }) {
  const ev: EvidenceInfo | null = evidenceInfo(d);
  if (!ev) return null;
  return (
    <div className="card">
      <h3 style={{ marginTop: 0 }}>금액 기준 문서와 증빙</h3>
      {!ev.counted && (
        <Msg kind="pending" title="확인 대기">
          {pendingText(ev.confirmation?.kind)}
          {ev.confirmation && ev.confirmation.missing.length > 0 && (
            <> 모르는 항목: {ev.confirmation.missing.map((m) => MISSING_LABEL[m] ?? m).join(", ")}.</>
          )}
        </Msg>
      )}
      {ev.amountConflict && (
        <Msg kind="failure" title="연결된 문서 간 금액 차이">
          같은 거래로 연결된 문서의 금액이 서로 다릅니다. 지급기한·지연이자는 계산하지 않았습니다.
        </Msg>
      )}
      <ul className="plain small">
        {ev.documents.map((x) => (
          <li key={`${x.entity}:${x.id}`}>
            <span className={`badge ${x.role === "basis" ? "b-ok" : "b-neutral"}`}>
              {EVIDENCE_ROLE_LABEL[x.role] ?? x.role}
            </span>{" "}
            {docName(x.entity, x.id)} · {won(x.amount)}
            {x.method ? <span className="hint"> ({LINK_METHOD_LABEL[x.method] ?? x.method})</span> : null}
          </li>
        ))}
      </ul>
      {ev.notes.length > 0 && (
        <ul className="plain small hint">
          {ev.notes.map((n, i) => (
            <li key={i}>{n}</li>
          ))}
        </ul>
      )}
      {ev.confirmation && ev.confirmation.choices.length > 0 && (
        <ConfirmLink d={d} ev={ev} reload={reload} reloading={reloading} />
      )}
    </div>
  );
}

function ConfirmLink({
  d,
  ev,
  reload,
  reloading,
}: {
  d: DecisionDetail;
  ev: EvidenceInfo;
  reload: () => void;
  reloading: boolean;
}) {
  const session = useSession();
  const lines = ev.confirmation?.candidateLines ?? [];
  const [line, setLine] = useState(lines[0] ?? "");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ kind: "ok" | "failure" | "pending"; text: string } | null>(null);
  if (!canWrite(session?.role)) return <p className="muted small">보기 전용 역할은 확인할 수 없습니다.</p>;

  async function send(relation: "same_sale" | "separate_sale") {
    setBusy(true);
    setMsg(null);
    try {
      const out = await api.confirmEvidenceLink(d.id, d.result_hash, {
        relation,
        settlement_line_id: relation === "same_sale" ? line : null,
      });
      setMsg({
        kind: "ok",
        text: `확인을 기록했습니다. 다시 계산된 결과 ${out.changed.length}건, 대체된 결과 ${out.removed.length}건.`,
      });
      reload();
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        setMsg({ kind: "pending", text: "보고 있던 결과가 그 사이 바뀌었습니다. 다시 불러왔으니 내용을 확인하세요." });
        reload();
      } else {
        setMsg({ kind: "failure", text: errorText(e) });
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <h3>{ev.confirmation?.kind === "duplicate_line" ? "같은 정산 행인지 확인" : "같은 거래인지 확인"}</h3>
      {msg && <Msg kind={msg.kind}>{msg.text}</Msg>}
      {lines.length > 0 && (
        <p className="row small">
          <label>
            정산 행{" "}
            <select value={line} onChange={(e) => setLine(e.target.value)} disabled={busy}>
              {lines.map((l) => (
                <option key={l} value={l}>
                  {l}
                </option>
              ))}
            </select>
          </label>
          <button onClick={() => send("same_sale")} disabled={busy || reloading || !line}>
            {ev.confirmation?.kind === "duplicate_line" ? "이 정산 행과 같은 행(중복)" : "이 정산 행과 같은 거래"}
          </button>
        </p>
      )}
      <p className="row small">
        <button className="secondary" onClick={() => send("separate_sale")} disabled={busy || reloading}>
          별개 거래(따로 미수로 계산)
        </button>
      </p>
    </>
  );
}

/** 약정 기한: 법정 기한과 따로 보여 주며, 법정 지연이율을 적용한 이자는 계산하지 않는다. */
function Contract({ info }: { info: ContractInfo | null }) {
  if (!info) return null;
  return (
    <>
      <h3>약정 기한(약정서 기준)</h3>
      {info.state === "conflict" && (
        <p className="small">적용 가능한 약정의 지급기한 일수가 서로 다릅니다: {info.values.join(", ")}일. 계산하지 않았습니다.</p>
      )}
      {info.state === "insufficient" && <p className="small">기준일 또는 거래 형태가 없어 계산하지 않았습니다.</p>}
      {info.state === "computed" && (
        <ul className="plain small">
          <li>
            약정 {info.termDays}일 → <b>{info.dueDate}</b> (기준일 {info.baseDate ?? "-"})
          </li>
          {info.comparison.map((c) => (
            <li key={c.label}>
              법정 기한({c.label === "rollover_on" ? "이월" : "말일"}) {c.statutoryDue}과 {c.differenceDays}일 차이
            </li>
          ))}
        </ul>
      )}
      <p className="hint">{info.interestNote}</p>
    </>
  );
}
