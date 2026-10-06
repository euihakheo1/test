/**
 * API 클라이언트의 쿠키 세션 동작 단위 테스트(node --test). fetch·document.cookie·저장소를 가짜로
 * 바꿔 요청 모양만 본다. 실제 서버와의 왕복은 e2e/(Playwright)가 확인한다.
 */
import assert from "node:assert/strict";
import { afterEach, beforeEach, test } from "node:test";

import { ApiError, CSRF_HEADER, api, loadSession, normalizeMe, readCsrfToken } from "./api.ts";
import {
  INVESTIGATION_STATUS_LABEL,
  NUMBERS_UNCHANGED_NOTE,
  anyActive,
  modeOptions,
  newestFirst,
  nextDelay,
  requiredDocuments,
  usageText,
} from "./investigate.ts";
import { forbiddenIn } from "./fmt.ts";
import { getSession, getSessionState, resetSessionForTests } from "./session.ts";

interface Call {
  url: string;
  method: string;
  headers: Record<string, string>;
  credentials: RequestCredentials | undefined;
  body: unknown;
}

type Handler = (c: Call) => Response | Promise<Response>;

const g = globalThis as unknown as Record<string, unknown>;
let calls: Call[] = [];
let writes: string[] = [];
const saved: Record<string, unknown> = {};

function recordingStorage(name: string): Storage {
  const data = new Map<string, string>();
  return {
    get length() {
      return data.size;
    },
    clear: () => data.clear(),
    getItem: (k: string) => data.get(k) ?? null,
    key: (i: number) => [...data.keys()][i] ?? null,
    removeItem: (k: string) => void data.delete(k),
    setItem: (k: string, v: string) => {
      writes.push(`${name}:${k}=${v}`);
      data.set(k, v);
    },
  };
}

function useFetch(h: Handler): void {
  g.fetch = async (input: string, init: RequestInit = {}) => {
    const c: Call = {
      url: String(input),
      method: init.method ?? "GET",
      headers: { ...((init.headers as Record<string, string>) ?? {}) },
      credentials: init.credentials,
      body: init.body,
    };
    calls.push(c);
    return h(c);
  };
}

const json = (status: number, body: unknown) =>
  new Response(body === undefined ? null : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });

const err = (status: number, code: string) => json(status, { error: { code, message: code, details: null, request_id: null } });

const ME = {
  user: { id: "u1", email: "a@example.com", role: "owner" },
  tenant: { id: "t1", name: "가나상사" },
};

beforeEach(() => {
  for (const k of ["fetch", "document", "localStorage", "sessionStorage"]) saved[k] = g[k];
  calls = [];
  writes = [];
  g.document = { cookie: "other=1; jt_csrf=csrf%2Dtoken-1; x=y" };
  g.localStorage = recordingStorage("local");
  g.sessionStorage = recordingStorage("session");
  resetSessionForTests();
});

afterEach(() => {
  for (const [k, v] of Object.entries(saved)) {
    if (v === undefined) delete g[k];
    else g[k] = v;
  }
});

test("readCsrfToken reads jt_csrf from document.cookie and returns null without it", () => {
  assert.equal(readCsrfToken(), "csrf-token-1");
  g.document = { cookie: "a=1" };
  assert.equal(readCsrfToken(), null);
  delete g.document;
  assert.equal(readCsrfToken(), null);
});

test("unsafe methods carry X-CSRF-Token from the cookie; GET does not; never an Authorization header", async () => {
  useFetch((c) => (c.method === "GET" ? json(200, { items: [], next_cursor: null }) : json(202, { job_id: "j1", status: "queued", status_url: "/x" })));
  await api.listJobs();
  await api.runAnalysis({ as_of: "2025-12-31" });
  await api.withdrawEvidenceLink("l1").catch(() => undefined);
  const [get, post, del] = calls;
  assert.equal(get.method, "GET");
  assert.equal(get.headers[CSRF_HEADER], undefined);
  assert.equal(post.method, "POST");
  assert.equal(post.headers[CSRF_HEADER], "csrf-token-1");
  assert.equal(del.method, "DELETE");
  assert.equal(del.headers[CSRF_HEADER], "csrf-token-1");
  for (const c of calls) {
    assert.equal(c.credentials, "include", `${c.method} ${c.url} must send cookies`);
    assert.equal(c.headers["Authorization"], undefined);
    assert.ok(c.url.startsWith("/api/v1/"), `same-origin path, got ${c.url}`);
  }
});

test("401 -> one POST /auth/refresh (with CSRF) -> the original request is retried once", async () => {
  let jobsCalls = 0;
  useFetch((c) => {
    if (c.url === "/api/v1/auth/refresh") return json(200, ME);
    jobsCalls += 1;
    return jobsCalls === 1 ? err(401, "unauthorized") : json(200, { items: [], next_cursor: null });
  });
  const page = await api.listJobs();
  assert.deepEqual(page.items, []);
  assert.deepEqual(
    calls.map((c) => `${c.method} ${c.url.split("?")[0]}`),
    ["GET /api/v1/jobs", "POST /api/v1/auth/refresh", "GET /api/v1/jobs"],
  );
  assert.equal(calls[1].headers[CSRF_HEADER], "csrf-token-1");
  assert.equal(calls[1].body, undefined, "the refresh token travels only in the HttpOnly cookie");
});

test("a second 401 after a successful refresh is not retried again", async () => {
  useFetch((c) => (c.url === "/api/v1/auth/refresh" ? json(200, ME) : err(401, "unauthorized")));
  await assert.rejects(api.listJobs(), (e: unknown) => e instanceof ApiError && e.status === 401);
  assert.equal(calls.filter((c) => c.url === "/api/v1/auth/refresh").length, 1);
  assert.equal(calls.length, 3);
  assert.equal(getSessionState().status, "anonymous");
});

test("failed refresh clears the client session and surfaces the 401", async () => {
  useFetch((c) => (c.url === "/api/v1/auth/refresh" ? err(401, "refresh_revoked") : err(401, "unauthorized")));
  await assert.rejects(api.decision("d:1#x"), (e: unknown) => e instanceof ApiError && e.status === 401);
  assert.equal(calls.length, 2);
  assert.equal(getSession(), null);
  assert.equal(getSessionState().status, "anonymous");
});

for (const status of [429, 500, 503]) {
  test(`refresh answered ${status} keeps the session (state "error"), never logs the user out`, async () => {
    useFetch((c) => (c.url === "/api/v1/auth/refresh" ? err(status, "too_many_requests") : err(401, "token_expired")));
    assert.equal(await loadSession(true), null);
    assert.equal(getSessionState().status, "error");
    await assert.rejects(
      api.listJobs(),
      (e: unknown) => e instanceof ApiError && e.code === "session_unavailable" && e.status === status,
    );
    assert.equal(getSessionState().status, "error");
  });
}

test("refresh that cannot reach the server keeps the session (state \"error\")", async () => {
  g.fetch = async (input: string) => {
    if (String(input) === "/api/v1/auth/refresh") throw new TypeError("fetch failed");
    return err(401, "token_expired");
  };
  assert.equal(await loadSession(true), null);
  assert.equal(getSessionState().status, "error");
});

test("concurrent 401s share a single refresh request", async () => {
  const seen = new Map<string, number>();
  let release!: () => void;
  const gate = new Promise<void>((r) => (release = r));
  useFetch(async (c) => {
    if (c.url === "/api/v1/auth/refresh") {
      await gate;
      return json(200, ME);
    }
    const n = (seen.get(c.url) ?? 0) + 1;
    seen.set(c.url, n);
    return n === 1 ? err(401, "unauthorized") : json(200, { items: [], next_cursor: null });
  });
  const a = api.listJobs();
  const b = api.listDecisions();
  await new Promise((r) => setTimeout(r, 10));
  release();
  await Promise.all([a, b]);
  assert.equal(calls.filter((c) => c.url === "/api/v1/auth/refresh").length, 1);
});

test("login/signup do not refresh on 401 and never persist tokens, even if the body has them", async () => {
  useFetch((c) => {
    if (c.url === "/api/v1/auth/login") {
      return json(200, { access_token: "SHOULD-NOT-BE-KEPT", refresh_token: "SHOULD-NOT-BE-KEPT", ...ME });
    }
    if (c.url === "/api/v1/auth/me") return json(200, ME);
    return err(404, "not_found");
  });
  const s = await api.login("a@example.com", "pw-1234567890");
  assert.ok(s);
  assert.deepEqual(s, {
    userId: "u1",
    email: "a@example.com",
    role: "owner",
    tenantId: "t1",
    tenantName: "가나상사",
    authMethod: null,
  });
  assert.deepEqual(writes, [], "nothing written to localStorage/sessionStorage");
  assert.ok(!JSON.stringify(getSessionState()).includes("SHOULD-NOT-BE-KEPT"));
  assert.ok(!JSON.stringify(getSessionState()).toLowerCase().includes("token"));

  calls = [];
  useFetch(() => err(401, "invalid_credentials"));
  await assert.rejects(api.login("a@example.com", "wrong"), (e: unknown) => e instanceof ApiError && e.status === 401);
  assert.equal(calls.length, 1, "no refresh attempt for a failed login");
});

test("logout posts with CSRF and clears client state even when the server fails", async () => {
  useFetch((c) => (c.url === "/api/v1/auth/me" ? json(200, ME) : json(204, undefined)));
  await loadSession();
  assert.equal(getSessionState().status, "authenticated");
  await api.logout();
  const out = calls.find((c) => c.url === "/api/v1/auth/logout");
  assert.ok(out);
  assert.equal(out.method, "POST");
  assert.equal(out.headers[CSRF_HEADER], "csrf-token-1");
  assert.equal(getSessionState().status, "anonymous");

  useFetch((c) => (c.url === "/api/v1/auth/me" ? json(200, ME) : err(500, "boom")));
  await loadSession(true);
  await api.logout();
  assert.equal(getSession(), null);
  assert.deepEqual(writes, []);
});

test("loadSession: 401 without a working refresh -> anonymous; network error -> error (not logged out)", async () => {
  useFetch(() => err(401, "unauthorized"));
  assert.equal(await loadSession(), null);
  assert.equal(getSessionState().status, "anonymous");

  resetSessionForTests();
  g.fetch = async () => {
    throw new TypeError("fetch failed");
  };
  assert.equal(await loadSession(), null);
  assert.equal(getSessionState().status, "error");
});

test("normalizeMe reads the {user, tenant} contract and the older flat form", () => {
  assert.deepEqual(normalizeMe(ME), {
    userId: "u1",
    email: "a@example.com",
    role: "owner",
    tenantId: "t1",
    tenantName: "가나상사",
    authMethod: null,
  });
  assert.deepEqual(
    normalizeMe({ user_id: "u2", email: "b@example.com", tenant_id: "t2", tenant_name: null, role: "viewer", auth_method: "cookie" }),
    { userId: "u2", email: "b@example.com", role: "viewer", tenantId: "t2", tenantName: null, authMethod: "cookie" },
  );
  assert.equal(normalizeMe({ user: { id: "u", email: "e", role: "member" }, tenant: { id: "t", role: "admin" } }).role, "member");
});

test("investigation requests follow the contract", async () => {
  useFetch((c) => {
    if (c.url.endsWith("/investigations") && c.method === "POST") return json(202, { investigation_id: "i1", job_id: "j1" });
    if (c.url.endsWith("/investigations")) return json(200, { items: [{ id: "i1" }] });
    return json(200, { modes_enabled: ["offline"], live_enabled: false });
  });
  const acc = await api.startInvestigation("d:1#a", { strategy: "roles", mode: "offline" });
  assert.equal(acc.investigation_id, "i1");
  const post = calls[0];
  assert.equal(post.url, "/api/v1/decisions/d%3A1%23a/investigations");
  assert.deepEqual(JSON.parse(String(post.body)), { strategy: "roles", mode: "offline" });
  assert.equal(post.headers[CSRF_HEADER], "csrf-token-1");
  assert.ok(post.headers["Idempotency-Key"]);
  assert.equal((await api.listInvestigations("d:1#a")).length, 1, "accepts {items: [...]} as well as a bare list");
  assert.equal((await api.investigationCapabilities()).live_enabled, false);
});

test("modeOptions: offline is always first; live only when the server enables it", () => {
  assert.deepEqual(modeOptions(null), ["offline"]);
  assert.deepEqual(modeOptions({ modes_enabled: [], live_enabled: false }), ["offline"]);
  assert.deepEqual(modeOptions({ modes_enabled: ["offline", "replay", "live"], live_enabled: false }), ["offline", "replay"]);
  assert.deepEqual(modeOptions({ modes_enabled: ["offline", "live"], live_enabled: true }), ["offline", "live"]);
  assert.deepEqual(modeOptions({ modes_enabled: ["offline"], live_enabled: true }), ["offline"]);
});

test("local inference is selectable only when the server explicitly enables it", () => {
  assert.deepEqual(modeOptions({ modes_enabled: ["offline", "local"], live_enabled: false }), ["offline"]);
  assert.deepEqual(modeOptions({ modes_enabled: ["offline", "local"], live_enabled: false, local_enabled: true }), ["offline", "local"]);
});

test("investigation helpers: polling, required documents, usage, ordering, wording", () => {
  assert.ok(anyActive([{ status: "succeeded" }, { status: "queued" }]));
  assert.ok(!anyActive([{ status: "succeeded" }, { status: "refused" }, { status: "failed" }]));
  assert.equal(nextDelay(1000), 1400);
  assert.equal(nextDelay(4500), 5000);
  assert.deepEqual(
    requiredDocuments({
      findings: [
        { kind: "required_document", message: "m", required_documents: ["입고 확인서", "판매마감 내역"], citations: [] },
        { kind: "note", message: "m", required_documents: ["입고 확인서"], citations: [] },
      ],
    }),
    ["입고 확인서", "판매마감 내역"],
  );
  assert.equal(
    usageText({ steps: 3, tool_calls: 4, input_tokens: 1200, output_tokens: 30, cost_krw: 0 }),
    "단계 3 · 도구 호출 4 · 입력 토큰 1,200 · 출력 토큰 30 · 비용 0원",
  );
  assert.equal(usageText(null), "사용량 정보 없음");
  assert.deepEqual(
    newestFirst([
      { id: "a", created_at: "2026-10-06T01:00:00Z" },
      { id: "b", created_at: "2026-10-06T02:00:00Z" },
    ]).map((x) => x.id),
    ["b", "a"],
  );
  for (const s of [NUMBERS_UNCHANGED_NOTE, ...Object.values(INVESTIGATION_STATUS_LABEL)]) {
    assert.deepEqual(forbiddenIn(s), []);
  }
  assert.match(NUMBERS_UNCHANGED_NOTE, /바뀌지 않습니다/);
});
