/**
 * 화면 표시용 로그인 상태. 토큰은 여기에 두지 않는다.
 *
 * 인증은 서버가 심는 HttpOnly 쿠키(jt_access, jt_refresh)로만 유지된다. 스크립트가 토큰을 읽거나
 * 저장하지 못하게 해서 XSS 한 번으로 장기 토큰이 빠져나가는 경로를 없애기 위해서다. 그래서 이 모듈은
 * GET /api/v1/auth/me 결과(사용자·회사·역할)만 메모리에 두고, localStorage·sessionStorage 를 쓰지 않는다.
 * 새로고침하면 상태는 "unknown" 으로 돌아가고 /auth/me 를 다시 부른다.
 * tenantId 는 표시용이다. 회사(테넌트)는 서버가 쿠키의 인증 정보로 정한다.
 */

export interface Session {
  userId: string;
  email: string;
  role: string;
  tenantId: string;
  tenantName: string | null;
  authMethod: string | null;
}

/** unknown: 아직 /auth/me 를 확인하지 않음. error: 서버 연결 실패(로그인 여부를 모름). */
export type SessionStatus = "unknown" | "authenticated" | "anonymous" | "error";

export interface SessionState {
  status: SessionStatus;
  session: Session | null;
}

type Listener = (s: SessionState) => void;
const listeners = new Set<Listener>();
const INITIAL: SessionState = { status: "unknown", session: null };
// useSyncExternalStore 는 같은 상태면 같은 객체를 돌려받아야 다시 그리지 않는다. 바뀔 때만 새 객체.
let state: SessionState = INITIAL;

function emit(next: SessionState): void {
  state = next;
  listeners.forEach((l) => l(state));
}

export function getSessionState(): SessionState {
  return state;
}

/** 서버 렌더링용 스냅숏(항상 unknown). */
export function getServerSessionState(): SessionState {
  return INITIAL;
}

export function getSession(): Session | null {
  return state.session;
}

export function setSession(s: Session): void {
  emit({ status: "authenticated", session: s });
}

/** 로그아웃·refresh 실패. 화면 상태만 지운다(쿠키 삭제는 서버 응답이 한다). */
export function clearSession(): void {
  emit({ status: "anonymous", session: null });
}

export function setSessionError(): void {
  emit({ status: "error", session: state.session });
}

/** 테스트 전용: 모듈 상태를 처음으로 되돌린다. */
export function resetSessionForTests(): void {
  emit(INITIAL);
}

export function onSession(l: Listener): () => void {
  listeners.add(l);
  return () => listeners.delete(l);
}

/** 역할 순서 viewer < member < admin < owner. */
export function canWrite(role: string | undefined): boolean {
  return role === "member" || role === "admin" || role === "owner";
}
