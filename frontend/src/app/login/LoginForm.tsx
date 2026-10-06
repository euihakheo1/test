"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { useState, type FormEvent } from "react";
import { Msg } from "@/components/Msg";
import { api } from "@/lib/api";
import { errorText } from "@/lib/fmt";

type Mode = "login" | "signup";

function safeNext(n: string | null): string {
  // 같은 사이트 안의 경로만 허용(오픈 리다이렉트 방지)
  return n && n.startsWith("/") && !n.startsWith("//") ? n : "/upload";
}

export default function LoginForm() {
  const router = useRouter();
  const params = useSearchParams();
  const [mode, setMode] = useState<Mode>("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [tenantName, setTenantName] = useState("");
  const [tenantId, setTenantId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const s =
        mode === "signup"
          ? await api.signup(email.trim(), password, tenantName.trim())
          : await api.login(email.trim(), password, tenantId.trim() || undefined);
      if (!s) {
        // 로그인 응답은 성공했지만 쿠키로 /auth/me 를 확인하지 못함: 쿠키 차단, 프록시가 Set-Cookie 를
        // 떨어뜨림, 운영에서 Secure 쿠키를 http 로 연 경우 등.
        setError("로그인 응답을 받았지만 로그인 상태를 확인하지 못했습니다. 브라우저 쿠키 설정과 접속 주소(https)를 확인하세요.");
        return;
      }
      router.push(safeNext(params.get("next")));
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card" style={{ maxWidth: 480 }}>
      <div className="tabs" role="tablist">
        <button role="tab" aria-selected={mode === "login"} onClick={() => setMode("login")} type="button">
          로그인
        </button>
        <button role="tab" aria-selected={mode === "signup"} onClick={() => setMode("signup")} type="button">
          가입(새 회사)
        </button>
      </div>
      <form onSubmit={submit}>
        <div className="field">
          <label htmlFor="email">이메일</label>
          <input id="email" type="email" autoComplete="email" required maxLength={320} value={email}
            onChange={(e) => setEmail(e.target.value)} />
        </div>
        <div className="field">
          <label htmlFor="pw">비밀번호</label>
          <input id="pw" type="password" required maxLength={1024}
            autoComplete={mode === "signup" ? "new-password" : "current-password"} value={password}
            onChange={(e) => setPassword(e.target.value)} />
        </div>
        {mode === "signup" ? (
          <div className="field">
            <label htmlFor="tn">회사 이름</label>
            <input id="tn" required maxLength={200} value={tenantName} onChange={(e) => setTenantName(e.target.value)} />
            <p className="hint">가입하면 새 회사(작업 공간)가 만들어지고 이 계정이 소유자가 됩니다. 회사별 자료는 서로 분리됩니다.</p>
          </div>
        ) : (
          <div className="field">
            <label htmlFor="tid">회사 ID (선택)</label>
            <input id="tid" maxLength={128} value={tenantId} onChange={(e) => setTenantId(e.target.value)} />
            <p className="hint">한 계정이 여러 회사에 속한 경우에만 입력합니다.</p>
          </div>
        )}
        {error && <Msg kind="failure">{error}</Msg>}
        <button type="submit" disabled={busy}>
          {busy ? "처리 중…" : mode === "signup" ? "가입" : "로그인"}
        </button>
      </form>
      <p className="hint" style={{ marginTop: "0.75rem" }}>
        로그인 상태는 스크립트가 읽을 수 없는 보안 쿠키(HttpOnly)로 유지되며, 로그아웃하면 서버에서 폐기됩니다.
      </p>
    </div>
  );
}
