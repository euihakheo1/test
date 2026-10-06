"use client";

import { useState, type FormEvent } from "react";
import { Msg } from "@/components/Msg";
import { ApiError, api, type PublicDueRequest, type PublicDueResponse } from "@/lib/api";
import { DueFormatError, validateDueInput } from "@/lib/due";
import {
  MISSING_LABEL,
  TRADE_TYPE_LABEL,
  UNRESOLVED_LABEL,
  VARIANT_LABEL,
  errorText,
  label,
  parseWon,
  won,
} from "@/lib/fmt";
import { RULE_INFO, splitRuleVersion } from "@/lib/view";

type TT = PublicDueRequest["trade_type"];
const BASE_LABEL: Record<TT, string> = {
  direct: "상품수령일(납품업자가 상품을 인도한 날)",
  consignment: "월 판매마감일",
  subcontract: "목적물 등의 수령일",
};

export default function CheckView() {
  const [tt, setTt] = useState<TT>("direct");
  const [base, setBase] = useState("");
  const [paid, setPaid] = useState("");
  const [unpaid, setUnpaid] = useState(false);
  const [asOf, setAsOf] = useState("");
  const [amount, setAmount] = useState("");
  const [roll, setRoll] = useState<"unset" | "on" | "off">("unset");
  const [rounding, setRounding] = useState<"floor" | "half_up">("floor");
  const [busy, setBusy] = useState(false);
  const [problems, setProblems] = useState<string[]>([]);
  const [fail, setFail] = useState<{ title: string; text: string } | null>(null);
  const [res, setRes] = useState<{ req: PublicDueRequest; out: PublicDueResponse } | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setFail(null);
    setRes(null);
    const amt = parseWon(amount);
    const req: PublicDueRequest = {
      trade_type: tt,
      base_date: base || null,
      paid_date: unpaid ? null : paid || null,
      as_of: unpaid ? asOf || null : null,
      amount: amt ?? -1,
      rollover: roll === "unset" ? null : roll === "on",
      rounding,
    };
    const p = validateDueInput(req);
    if (amt === null) p.push("금액을 원 단위 숫자로 입력하세요(예: 10,000,000).");
    if (!unpaid && !paid) p.push("지급일을 입력하거나 '아직 지급되지 않음'을 고르세요.");
    setProblems(p);
    if (p.length) return;
    setBusy(true);
    try {
      setRes({ req, out: await api.publicDue(req) });
    } catch (err) {
      if (err instanceof ApiError && (err.status === 404 || err.status === 405)) {
        setFail({
          title: "처리 실패",
          text: "이 서버는 공개 계산 기능(POST /api/v1/public/due)을 제공하지 않습니다. 같은 계산은 명령줄에서 `jettae rules due` 로 할 수 있습니다.",
        });
      } else if (err instanceof DueFormatError) {
        setFail({ title: "처리 실패", text: `서버 응답을 해석하지 못했습니다: ${err.message}` });
      } else {
        setFail({ title: "처리 실패", text: errorText(err) });
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <form className="card" onSubmit={submit} noValidate>
        <div className="grid2">
          <div className="field">
            <label htmlFor="tt">거래 유형</label>
            <select id="tt" value={tt} onChange={(e) => setTt(e.target.value as TT)}>
              {(Object.keys(TRADE_TYPE_LABEL) as TT[]).map((k) => (
                <option key={k} value={k}>
                  {TRADE_TYPE_LABEL[k]}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label htmlFor="base">기준일: {BASE_LABEL[tt]}</label>
            <input id="base" type="date" value={base} onChange={(e) => setBase(e.target.value)} />
            <p className="hint">
              모르면 비워 두세요. 세금계산서 작성일로 대신하지 않으며, 계산 대신 필요한 서류를 안내합니다.
            </p>
          </div>
          <div className="field">
            <label htmlFor="amt">금액(원)</label>
            <input
              id="amt"
              inputMode="numeric"
              placeholder="10,000,000"
              value={amount}
              onChange={(e) => setAmount(e.target.value)}
            />
          </div>
          <div className="field">
            <label htmlFor="paid">지급일</label>
            <input id="paid" type="date" value={paid} disabled={unpaid} onChange={(e) => setPaid(e.target.value)} />
            <label className="inline" style={{ marginTop: "0.35rem" }}>
              <input type="checkbox" checked={unpaid} onChange={(e) => setUnpaid(e.target.checked)} /> 아직 지급되지 않음
            </label>
            {unpaid && (
              <>
                <label htmlFor="asof">계산 기준일(비우면 오늘)</label>
                <input id="asof" type="date" value={asOf} onChange={(e) => setAsOf(e.target.value)} />
              </>
            )}
          </div>
          <div className="field">
            <label htmlFor="roll">기한 말일이 휴일일 때</label>
            <select id="roll" value={roll} onChange={(e) => setRoll(e.target.value as "unset" | "on" | "off")}>
              <option value="unset">정하지 않음 — 두 계산을 함께 표시</option>
              <option value="off">말일 그대로</option>
              <option value="on">다음 영업일로 이월</option>
            </select>
          </div>
          <div className="field">
            <label htmlFor="rnd">원 단위 처리</label>
            <select id="rnd" value={rounding} onChange={(e) => setRounding(e.target.value as "floor" | "half_up")}>
              <option value="floor">버림</option>
              <option value="half_up">반올림</option>
            </select>
          </div>
        </div>
        {problems.length > 0 && (
          <Msg kind="failure" title="입력 확인">
            <ul className="plain">
              {problems.map((p) => (
                <li key={p}>{p}</li>
              ))}
            </ul>
          </Msg>
        )}
        <button type="submit" disabled={busy}>
          {busy ? "계산 중…" : "계산"}
        </button>
      </form>

      {fail && <Msg kind="failure" title={fail.title}>{fail.text}</Msg>}
      {res && <Result out={res.out} />}

      <p className="notice">
        이 계산은 대규모유통업법 제8조·하도급법 제13조와 지연이율 고시(연 15.5%)를 입력값에 그대로 적용한 결과이며 법적
        판단이 아닙니다. 약정·정산 주기·실제 수령일 등 확인되지 않은 조건에 따라 달라질 수 있습니다. 2026년 개정안(직매입
        35일 등)은 시행일이 정해지지 않아 적용하지 않습니다.
      </p>
    </>
  );
}

function Result({ out }: { out: PublicDueResponse }) {
  if (out.result === "insufficient") {
    return (
      <div className="card">
        <Msg kind="pending" title="판단 보류">
          계산에 필요한 기준일이 없어 지급기한을 계산하지 않았습니다.
        </Msg>
        <dl className="kv">
          <dt>미확인 항목</dt>
          <dd>{out.missing.map((m) => label(MISSING_LABEL, m)).join(", ") || "-"}</dd>
          <dt>필요 서류</dt>
          <dd>
            <ul className="plain">
              {out.required_documents.map((d) => (
                <li key={d}>{d}</li>
              ))}
            </ul>
          </dd>
        </dl>
        {out.notes.length > 0 && <p className="hint">{out.notes.join(" / ")}</p>}
      </div>
    );
  }
  return (
    <div className="card">
      <h2 style={{ marginTop: 0 }}>계산 결과</h2>
      <dl className="kv">
        <dt>거래 유형</dt>
        <dd>
          {label(TRADE_TYPE_LABEL, out.trade_type)} · 기준일 {out.base_date} + {out.term_days}일
        </dd>
        <dt>원금</dt>
        <dd>{won(out.principal)}</dd>
        <dt>지연 계산 종료일</dt>
        <dd>{out.end_date ?? "-"}</dd>
        <dt>원 단위 처리</dt>
        <dd>{out.rounding === "half_up" ? "반올림" : "버림"}</dd>
      </dl>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>계산 방식</th>
              <th>지급기한</th>
              <th className="num">지연일수</th>
              <th className="num">지연이자</th>
            </tr>
          </thead>
          <tbody>
            {out.variants.map((v) => (
              <tr key={v.label}>
                <td>{label(VARIANT_LABEL, v.label)}</td>
                <td>{v.due_date}</td>
                <td className="num">{v.delay_days}일</td>
                <td className="num">{won(v.interest)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {out.unresolved.length > 0 && (
        <Msg kind="pending" title="확인이 필요한 조건">
          {out.unresolved.map((u) => label(UNRESOLVED_LABEL, u)).join(", ")} — 확인되기 전까지 두 계산을 함께
          보여 줍니다.
        </Msg>
      )}
      <h3>계산 가정</h3>
      <ul className="plain small">
        {out.assumptions.map((a) => (
          <li key={a}>{a}</li>
        ))}
      </ul>
      <h3>관련 근거</h3>
      <ul className="plain small">
        {[out.rule_version, out.interest_rule_version]
          .filter((x): x is string => Boolean(x))
          .map((rv) => {
            const { id, version } = splitRuleVersion(rv);
            return (
              <li key={rv}>
                {RULE_INFO[id]?.title ?? id} <span className="mono">({version})</span>
              </li>
            );
          })}
        {out.source_urls.map((u) => (
          <li key={u}>
            <a href={u} target="_blank" rel="noopener noreferrer">
              {u}
            </a>
          </li>
        ))}
      </ul>
    </div>
  );
}
