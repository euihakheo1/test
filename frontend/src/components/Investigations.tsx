"use client";

import { useEffect, useState } from "react";
import {
  api,
  type Investigation,
  type InvestigationCapabilities,
  type InvestigationMode,
  type InvestigationStrategy,
} from "@/lib/api";
import { describeLocator, errorText, kst, shortHash } from "@/lib/fmt";
import {
  FINDING_KIND_LABEL,
  INVESTIGATION_STATUS_LABEL,
  MODE_LABEL,
  NUMBERS_UNCHANGED_NOTE,
  STRATEGY_LABEL,
  anyActive,
  errorMessage,
  modeOptions,
  newestFirst,
  nextDelay,
  requiredDocuments,
  usageText,
} from "@/lib/investigate";
import { canWrite } from "@/lib/session";
import { Loading, Msg } from "./Msg";
import { useSession } from "./useSession";

const STATUS_BADGE: Record<string, string> = {
  queued: "b-info",
  running: "b-info",
  succeeded: "b-ok",
  failed: "b-bad",
  refused: "b-warn",
};

/**
 * 결과 상세의 Agent 조사. 조사는 worker 의 investigate_decision 작업으로 돌고, 이 화면은 목록을
 * 폴링한다. 서버가 조사 기능을 제공하지 않으면(404) 이 영역만 "제공하지 않음"으로 보여 준다.
 */
export function Investigations({ decisionId }: { decisionId: string }) {
  const session = useSession();
  const [caps, setCaps] = useState<InvestigationCapabilities | null>(null);
  const [capsError, setCapsError] = useState<unknown>(null);
  const [list, setList] = useState<Investigation[] | null>(null);
  const [listError, setListError] = useState<unknown>(null);
  const [tick, setTick] = useState(0);
  const [strategy, setStrategy] = useState<InvestigationStrategy>("single");
  const [mode, setMode] = useState<InvestigationMode>("offline");
  const [busy, setBusy] = useState(false);
  const [startError, setStartError] = useState<unknown>(null);

  useEffect(() => {
    let alive = true;
    api.investigationCapabilities().then(
      (c) => alive && setCaps(c),
      (e: unknown) => alive && setCapsError(e),
    );
    return () => {
      alive = false;
    };
  }, []);

  // 목록 읽기 + 진행 중인 조사가 있으면 끝날 때까지 폴링(1초→최대 5초). 화면을 떠나면 멈춘다.
  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const ctl = new AbortController();
    let delay = 1000;
    const load = async () => {
      try {
        const items = await api.listInvestigations(decisionId, ctl.signal);
        if (!alive) return;
        setList(items);
        setListError(null);
        if (!anyActive(items)) return;
      } catch (e) {
        if (!alive) return;
        setListError(e);
      }
      delay = nextDelay(delay);
      timer = setTimeout(load, delay);
    };
    void load();
    return () => {
      alive = false;
      ctl.abort();
      if (timer) clearTimeout(timer);
    };
  }, [decisionId, tick]);

  const options = modeOptions(caps);
  const selectedMode = options.includes(mode) ? mode : "offline";
  const unavailable =
    (capsError && typeof capsError === "object" && "status" in capsError && capsError.status === 404) ||
    (listError && typeof listError === "object" && "status" in listError && listError.status === 404);

  async function start() {
    setBusy(true);
    setStartError(null);
    try {
      await api.startInvestigation(decisionId, { strategy, mode: selectedMode });
      setTick((n) => n + 1);
    } catch (e) {
      setStartError(e);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card" data-testid="investigations">
      <h3 style={{ marginTop: 0 }}>Agent 조사(참고)</h3>
      <p className="small">
        Agent 가 이 결과의 사실·원문 위치·필요 서류를 다시 살펴 확인할 점을 정리합니다. {NUMBERS_UNCHANGED_NOTE}
      </p>
      {unavailable ? (
        <Msg kind="empty" title="서버 미제공">
          이 서버는 Agent 조사 기능을 제공하지 않습니다.
        </Msg>
      ) : (
        <>
          {canWrite(session?.role) ? (
            <div className="row small">
              <label>
                방식{" "}
                <select
                  aria-label="조사 방식"
                  value={strategy}
                  onChange={(e) => setStrategy(e.target.value as InvestigationStrategy)}
                  disabled={busy}
                >
                  <option value="single">{STRATEGY_LABEL.single}</option>
                  <option value="roles">{STRATEGY_LABEL.roles}</option>
                </select>
              </label>
              <label>
                실행{" "}
                <select
                  aria-label="실행 모드"
                  value={selectedMode}
                  onChange={(e) => setMode(e.target.value as InvestigationMode)}
                  disabled={busy}
                >
                  {options.map((m) => (
                    <option key={m} value={m}>
                      {MODE_LABEL[m] ?? m}
                    </option>
                  ))}
                </select>
              </label>
              <button onClick={start} disabled={busy}>
                {busy ? "요청 중…" : "조사 실행"}
              </button>
            </div>
          ) : (
            <p className="muted small">보기 전용 역할은 조사를 실행할 수 없습니다.</p>
          )}
          {caps && !caps.live_enabled && (
            <p className="hint">
              실제 LLM 호출은 서버에서 예산과 사용 설정을 켠 경우에만 선택할 수 있습니다. 기본은 LLM 호출이 없는
              오프라인 방식입니다.
            </p>
          )}
          {startError !== null && <Msg kind="failure">{errorText(startError)}</Msg>}
          {listError !== null && <Msg kind="failure">{errorText(listError)}</Msg>}
          {list === null && listError === null ? (
            <Loading />
          ) : list && list.length === 0 ? (
            <p className="muted small">아직 실행한 조사가 없습니다.</p>
          ) : list ? (
            <ul className="inv-list">
              {newestFirst(list).map((inv) => (
                <InvestigationItem key={inv.id} inv={inv} />
              ))}
            </ul>
          ) : null}
        </>
      )}
    </div>
  );
}

function InvestigationItem({ inv }: { inv: Investigation }) {
  const docs = requiredDocuments(inv);
  const err = errorMessage(inv.error);
  return (
    <li className="inv-item" data-testid="investigation" data-status={inv.status}>
      <div className="row small">
        <span className={`badge ${STATUS_BADGE[inv.status] ?? "b-neutral"}`}>
          {INVESTIGATION_STATUS_LABEL[inv.status] ?? inv.status}
        </span>
        <span>{STRATEGY_LABEL[inv.strategy] ?? inv.strategy}</span>
        <span className="muted">· {inv.mode}</span>
        <span className="muted">· {kst(inv.created_at)}</span>
      </div>
      {inv.status === "refused" && (
        <Msg kind="pending" title="실행하지 않음">
          서버 설정(예산·사용 허용)상 이 방식으로 실행하지 않았습니다. 유료 호출은 일어나지 않았습니다.
          {err ? ` (${err})` : ""}
        </Msg>
      )}
      {inv.status === "failed" && <Msg kind="failure">{err ?? "조사를 마치지 못했습니다."}</Msg>}
      {docs.length > 0 && (
        <div className="small" data-testid="investigation-required-documents">
          <b>필요 서류</b>
          <ul className="plain">
            {docs.map((d) => (
              <li key={d}>{d}</li>
            ))}
          </ul>
        </div>
      )}
      {inv.findings.length > 0 && (
        <ol className="small inv-finding">
          {inv.findings.map((f, i) => (
            <li key={i}>
              <b>{FINDING_KIND_LABEL[f.kind] ?? f.kind}</b>: {f.message}
              {f.citations.map((c, j) => (
                <blockquote key={j} className="inv-cite">
                  <span className="hint">
                    문서 버전 <span className="mono">{shortHash(c.doc_version_id, 12)}</span> ·{" "}
                    {describeLocator(c.locator)}
                  </span>
                  <br />
                  <span className="mono">{c.excerpt}</span>
                </blockquote>
              ))}
            </li>
          ))}
        </ol>
      )}
      {inv.status === "succeeded" && inv.findings.length === 0 && (
        <p className="muted small">추가로 확인할 점을 찾지 못했습니다.</p>
      )}
      <p className="hint">{usageText(inv.usage)}</p>
    </li>
  );
}
