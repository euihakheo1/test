"use client";

import { useEffect, useSyncExternalStore } from "react";
import { loadSession } from "@/lib/api";
import {
  getServerSessionState,
  getSessionState,
  onSession,
  type Session,
  type SessionState,
} from "@/lib/session";

const subscribe = (cb: () => void) => onSession(() => cb());

/**
 * 쿠키 로그인 상태. 처음 쓰일 때 GET /auth/me 로 확인한다(여러 컴포넌트가 불러도 요청은 한 번).
 * 서버 렌더링 중에는 항상 unknown 이다(쿠키를 화면 서버에서 읽지 않는다).
 */
export function useSessionState(): SessionState {
  const st = useSyncExternalStore(subscribe, getSessionState, getServerSessionState);
  useEffect(() => {
    if (st.status === "unknown") void loadSession();
  }, [st.status]);
  return st;
}

/** 현재 로그인 정보(확인 전·비로그인이면 null). */
export function useSession(): Session | null {
  return useSessionState().session;
}
