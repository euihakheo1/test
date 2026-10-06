"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { api } from "@/lib/api";
import { useSessionState } from "./useSession";

const LINKS: [string, string][] = [
  ["/upload", "문서 업로드"],
  ["/analysis", "분석"],
  ["/results", "결과"],
  ["/changes", "정정·추가 자료"],
  ["/report", "보고서"],
  ["/check", "무료 검산"],
];

export function Nav() {
  const path = usePathname() ?? "/";
  const { status, session } = useSessionState();
  const router = useRouter();
  return (
    <header className="nav">
      <nav className="nav-in" aria-label="주 메뉴">
        <Link href="/" className="brand">
          제때받기
        </Link>
        <div className="nav-links">
          {LINKS.map(([href, text]) => (
            <Link key={href} href={href} aria-current={path.startsWith(href) ? "page" : undefined}>
              {text}
            </Link>
          ))}
        </div>
        <div className="nav-user">
          {status === "authenticated" && session ? (
            <>
              <span className="nav-tenant" data-testid="nav-tenant" title={`회사 ID ${session.tenantId}`}>
                {session.tenantName ?? session.tenantId}
              </span>
              <span>
                {session.email} · {session.role}
              </span>
              <button
                className="secondary"
                onClick={async () => {
                  await api.logout();
                  router.replace("/login");
                }}
              >
                로그아웃
              </button>
            </>
          ) : status === "anonymous" || status === "error" ? (
            <Link href="/login">로그인·가입</Link>
          ) : null}
        </div>
      </nav>
    </header>
  );
}
