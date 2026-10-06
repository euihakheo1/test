"use client";

import { useState } from "react";
import { api, type DocumentVersion, type FactOut } from "@/lib/api";
import { decodeText, describeLocator, errorText, showValue, sliceContext } from "@/lib/fmt";
import { Msg } from "./Msg";

/**
 * 한 문서 버전에서 나온 사실들의 원문 위치.
 * - 모든 형식: 위치 설명(시트/행/열 또는 페이지/문자 범위)과 발췌.
 * - CSV·TXT: 원본 파일을 받아 문자 범위를 앞뒤 문맥과 함께 강조(발췌와 다르면 경고).
 */
export function SourcePreview({
  dv,
  doc,
  facts,
}: {
  dv: string;
  doc: DocumentVersion | null;
  facts: FactOut[];
}) {
  const [text, setText] = useState<{ text: string; encoding: string } | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const isText = doc ? /\.(csv|txt)$/i.test(doc.filename) || doc.media_type.startsWith("text/") : false;

  async function load() {
    setBusy(true);
    setErr(null);
    try {
      const t = decodeText(await api.content(dv));
      if (!t) setErr("파일 인코딩을 알아보지 못했습니다.");
      else setText(t);
    } catch (e) {
      setErr(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card">
      <div className="row">
        <b>{doc ? `${doc.filename} (v${doc.version})` : "문서"}</b>
        <span className="hint mono">{dv}</span>
        {isText && !text && (
          <button className="secondary" onClick={load} disabled={busy}>
            {busy ? "불러오는 중…" : "원문에서 위치 보기"}
          </button>
        )}
      </div>
      {!isText && doc && (
        <p className="hint">엑셀·PDF는 위치(시트·셀 또는 쪽·문자 범위)와 발췌만 보여 줍니다. 원본은 해당 위치에서 확인하세요.</p>
      )}
      {err && <Msg kind="failure">{err}</Msg>}
      <div className="table-wrap" style={{ marginTop: "0.5rem" }}>
        <table>
          <thead>
            <tr>
              <th>사실</th>
              <th>값</th>
              <th>원문 위치</th>
              <th>발췌 / 원문</th>
            </tr>
          </thead>
          <tbody>
            {facts.map((f) => {
              const loc = f.span?.locator ?? {};
              const cs = loc.char_start;
              const ce = loc.char_end;
              const ctx =
                text && typeof cs === "number" && typeof ce === "number"
                  ? sliceContext(text.text, cs, ce, f.span?.excerpt ?? "")
                  : null;
              return (
                <tr key={f.id}>
                  <td className="small">
                    {f.kind}
                    <div className="hint">{f.extractor}</div>
                  </td>
                  <td>{showValue(f.value)}</td>
                  <td className="small">{describeLocator(f.span?.locator)}</td>
                  <td>
                    {ctx ? (
                      <>
                        <div className="src">
                          {ctx.before}
                          <mark>{ctx.hit}</mark>
                          {ctx.after}
                        </div>
                        {!ctx.matches && (
                          <div className="hint" style={{ color: "var(--bad)" }}>
                            원문의 이 범위가 발췌(&ldquo;{f.span?.excerpt}&rdquo;)와 다릅니다. 위치를 직접 확인하세요.
                          </div>
                        )}
                      </>
                    ) : (
                      <div className="src">{f.span?.excerpt || "(발췌 없음)"}</div>
                    )}
                    {text && typeof cs === "number" && !ctx && (
                      <div className="hint">문자 범위가 원문 길이를 벗어나 위치를 표시하지 못했습니다.</div>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {text && <p className="hint">원문 인코딩: {text.encoding}</p>}
    </div>
  );
}
