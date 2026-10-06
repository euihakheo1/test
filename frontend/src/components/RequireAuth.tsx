"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, type ReactNode } from "react";
import { loadSession } from "@/lib/api";
import { Loading, Msg } from "./Msg";
import { useSessionState } from "./useSession";

/**
 * 로그인한 경우에만 children 을 보여 준다. 공개 페이지(무료 검산)에는 쓰지 않는다.
 * 비로그인으로 확인되면 /login?next=<현재 경로> 로 보낸다. 화면 숨김은 편의일 뿐이고
 * 접근 통제는 서버가 쿠키로 한다(이 컴포넌트를 우회해도 API 는 401 을 준다).
 */
export function RequireAuth({ children }: { children: ReactNode }) {
  const st = useSessionState();
  const path = usePathname() ?? "/";
  const router = useRouter();
  const loginHref = `/login?next=${encodeURIComponent(path)}`;

  useEffect(() => {
    if (st.status !== "anonymous") return;
    // 쿼리(?id= 등)까지 되돌아올 수 있게 실제 주소를 쓴다. useSearchParams 는 Suspense 경계를
    // 요구하므로 여기서는 쓰지 않는다(이 컴포넌트는 페이지 최상단에 놓인다).
    const { pathname, search } = window.location;
    router.replace(`/login?next=${encodeURIComponent(pathname + search)}`);
  }, [st.status, router]);

  if (st.status === "authenticated") return <>{children}</>;
  if (st.status === "error") {
    return (
      <Msg kind="failure" title="로그인 상태를 확인하지 못함">
        서버에 연결하지 못했습니다.{" "}
        <button className="secondary" onClick={() => void loadSession(true)}>
          다시 시도
        </button>
      </Msg>
    );
  }
  if (st.status === "anonymous") {
    return (
      <Msg kind="info" title="로그인 필요">
        이 화면은 로그인 후 사용할 수 있습니다. <Link href={loginHref}>로그인·가입</Link>
        {" · "}
        <Link href="/check">로그인 없이 무료 검산</Link>
      </Msg>
    );
  }
  return <Loading />;
}
