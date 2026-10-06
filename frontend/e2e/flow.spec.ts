/**
 * 실제 프로세스(Next → API → worker, SQLite)로 화면 흐름을 확인한다. 입력은 e2e/fixtures 의 손으로 쓴
 * 두 줄짜리 CSV 뿐이며, 이 테스트가 통과해도 계산 정확도나 운영 보안 전체를 보증하지는 않는다.
 */
import { expect, request as apiRequest, test, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import path from "node:path";

const FIX = path.join(__dirname, "fixtures");
const PASSWORD = "e2e-pass-0123456789";
// 첫 테스트가 만든 결정 ID. 다른 회사로 가입한 테스트가 이 ID 를 읽지 못하는지(조직 격리) 확인한다.
let firstTenantDecisionId = "";

function uniqueEmail(tag: string): string {
  return `e2e-${tag}-${Date.now()}-${Math.floor(Math.random() * 1e6)}@example.com`;
}

async function signup(page: Page, email: string, tenant: string): Promise<void> {
  await page.goto("/login");
  await page.getByRole("tab", { name: "가입(새 회사)" }).click();
  await page.locator("#email").fill(email);
  await page.locator("#pw").fill(PASSWORD);
  await page.locator("#tn").fill(tenant);
  const resp = page.waitForResponse((r) => r.url().endsWith("/api/v1/auth/signup"));
  await page.getByRole("button", { name: "가입", exact: true }).click();
  const r = await resp;
  expect(r.status()).toBe(201);
  // 쿠키 세션 계약: 응답 본문에 access/refresh 토큰을 싣지 않는다.
  const text = await r.text();
  expect(text).not.toContain("access_token");
  expect(text).not.toContain("refresh_token");
  await expect(page).toHaveURL(/\/upload$/);
  await expect(page.getByTestId("nav-tenant")).toHaveText(tenant);
}

/** 작업 상세 화면에서 작업이 끝날 때까지 기다리고 완료 배지를 확인한다. */
async function waitJobSucceeded(page: Page): Promise<void> {
  await expect(page).toHaveURL(/\/job\?id=/);
  await expect(page.locator(".badge", { hasText: /^(완료|처리 실패|취소됨)$/ }).first()).toBeVisible({
    timeout: 60_000,
  });
  await expect(page.locator(".badge", { hasText: /^완료$/ }).first()).toBeVisible();
}

test("signup → upload → mapping → analysis → detail → investigation → correction → approve → export", async ({
  page,
}) => {
  const tenant = `E2E 상사 ${Date.now()}`;
  await signup(page, uniqueEmail("flow"), tenant);

  await test.step("upload a settlement CSV the parser cannot recognise by itself", async () => {
    await page.locator('input[type="file"]').setInputFiles(path.join(FIX, "settlement_v1.csv"));
    await page.locator("#kind").selectOption("settlement");
    await page.getByRole("button", { name: "올리기", exact: true }).click();
    await expect(page.getByText("저장함")).toBeVisible();
    await page.getByRole("link", { name: "읽기 작업 상태 보기" }).click();
    await waitJobSucceeded(page);
    await page.getByRole("link", { name: "열 매핑 확인하기" }).click();
  });

  await test.step("confirm the column mapping and re-read", async () => {
    await expect(page).toHaveURL(/\/mapping\?dv=/);
    await page.locator("#f-counterparty").selectOption("0");
    await page.locator("#f-amount").selectOption("3");
    await page.locator("#f-trade_type").selectOption("1");
    await page.getByRole("button", { name: "매핑 확정하고 다시 읽기" }).click();
    await waitJobSucceeded(page);
    await expect(page.getByText(/양식을 읽었습니다\(거래 1건/)).toBeVisible();
  });

  let decisionUrl = "";
  let decisionId = "";
  await test.step("run the analysis and open the result", async () => {
    await page.getByRole("link", { name: "분석", exact: true }).click();
    await page.locator("#asof").fill("2025-12-31");
    await page.getByRole("button", { name: "분석 실행" }).click();
    await waitJobSucceeded(page);
    await expect(page.getByText("거래 1건을 분석했습니다.")).toBeVisible();
    await page.getByRole("link", { name: "결과 목록 보기" }).click();
    await expect(page).toHaveURL(/\/results/);
    await page.locator('a[href^="/decision?id="]').first().click();
    await expect(page).toHaveURL(/\/decision\?id=/);
    decisionUrl = page.url();
    decisionId = new URL(decisionUrl).searchParams.get("id") ?? "";
    expect(decisionId).not.toBe("");
    firstTenantDecisionId = decisionId;
  });

  await test.step("decision detail shows evidence, source positions and required documents", async () => {
    await expect(page.getByRole("heading", { name: "금액 기준 문서와 증빙" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "사실과 원문 위치" })).toBeVisible();
    await expect(page.getByText("1000000").first()).toBeVisible();
    // 상품수령일 열이 없으므로 지급기한은 계산하지 않고 필요 서류를 안내한다.
    await expect(page.getByText("계산 보류").first()).toBeVisible();
    const reqCard = page.locator(".card", { has: page.getByRole("heading", { name: "필요 서류" }) });
    await expect(reqCard.locator("li").first()).toBeVisible();
  });

  await test.step("offline investigation lists required documents and leaves engine numbers unchanged", async () => {
    const before = await (await page.request.get(`/api/v1/decisions/${encodeURIComponent(decisionId)}`)).json();
    const box = page.getByTestId("investigations");
    await expect(box.getByText("바뀌지 않습니다")).toBeVisible();
    await expect(box.getByLabel("실행 모드")).toHaveValue("offline");
    await box.getByLabel("조사 방식").selectOption("single");
    await box.getByRole("button", { name: "조사 실행" }).click();
    const done = box.locator('[data-testid="investigation"][data-status="succeeded"]').first();
    await expect(done).toBeVisible({ timeout: 60_000 });
    await expect(done.getByTestId("investigation-required-documents").locator("li").first()).toBeVisible();
    await expect(done.getByText(/비용 0원/)).toBeVisible();
    const after = await (await page.request.get(`/api/v1/decisions/${encodeURIComponent(decisionId)}`)).json();
    expect(after.result_hash).toBe(before.result_hash);
  });

  await test.step("approve the current result (CSRF header sent from the cookie)", async () => {
    const req = page.waitForRequest((r) => r.method() === "POST" && r.url().includes("/approvals"));
    await page.getByRole("button", { name: "이 결과 확인(승인)" }).click();
    expect((await req).headers()["x-csrf-token"]).toBeTruthy();
    await expect(page.getByRole("button", { name: "현재 결과 확인됨" })).toBeVisible();
  });

  await test.step("upload a corrected version of the same document", async () => {
    await page.getByRole("link", { name: "정정·추가 자료", exact: true }).click();
    await page.locator('input[type="file"]').setInputFiles(path.join(FIX, "settlement_v2.csv"));
    await page.locator("#ckind").selectOption("settlement");
    const target = page.locator("#ctarget option", { hasText: "settlement_v1.csv" });
    await page.locator("#ctarget").selectOption((await target.getAttribute("value")) ?? "");
    await page.getByRole("button", { name: "올리고 영향 비교" }).click();
    await expect(page.locator(".badge", { hasText: /^완료$/ }).first()).toBeVisible({ timeout: 60_000 });
  });

  await test.step("the approved result now needs review; approve again", async () => {
    await page.goto(decisionUrl);
    // 배지와 안내 상자의 제목으로 확인한다(승인 설명 문장에도 같은 낱말이 있어 본문 검색은 쓰지 않음).
    await expect(page.locator(".msg-title", { hasText: "재확인 필요" })).toBeVisible();
    await expect(page.locator(".badge", { hasText: /^재확인 필요$/ }).first()).toBeVisible();
    const cur = await (await page.request.get(`/api/v1/decisions/${encodeURIComponent(decisionId)}`)).json();
    expect(cur.review_status).toBe("REVIEW_REQUIRED");
    await expect(page.getByText("특약매입").first()).toBeVisible();
    const req = page.waitForRequest((r) => r.method() === "POST" && r.url().includes("/approvals"));
    await page.getByRole("button", { name: "이 결과 확인(승인)" }).click();
    expect((await req).headers()["x-csrf-token"]).toBeTruthy();
    await expect(page.getByRole("button", { name: "현재 결과 확인됨" })).toBeVisible();
    const after = await (await page.request.get(`/api/v1/decisions/${encodeURIComponent(decisionId)}`)).json();
    expect(after.review_status).toBe("APPROVED");
  });

  await test.step("export the report of approved results", async () => {
    await page.getByRole("link", { name: "보고서", exact: true }).click();
    const only = page.getByLabel("현재 확인(승인)된 결과만 허용");
    if (!(await only.isChecked())) await only.check();
    const dl = page.waitForEvent("download");
    await page.getByRole("button", { name: "보고서 내려받기" }).click();
    const file = await dl;
    expect(file.suggestedFilename()).toMatch(/\.csv$/);
    const body = readFileSync((await file.path()) ?? "", "utf-8");
    expect(body.length).toBeGreaterThan(0);
    await expect(page.getByText(/내려받았습니다/)).toBeVisible();
  });
});

test("cookies are HttpOnly, unsafe API calls need the CSRF header, logout ends the session", async ({
  page,
  context,
}) => {
  await signup(page, uniqueEmail("auth"), `E2E 인증 ${Date.now()}`);

  const cookies = await context.cookies();
  const byName = new Map(cookies.map((c) => [c.name, c]));
  expect(byName.get("jt_access")?.httpOnly).toBe(true);
  expect(byName.get("jt_refresh")?.httpOnly).toBe(true);
  expect(byName.get("jt_csrf")?.httpOnly).toBe(false);
  // 토큰은 스크립트 저장소에 남지 않는다.
  const stored = await page.evaluate(() => JSON.stringify({ l: { ...localStorage }, s: { ...sessionStorage } }));
  for (const name of ["jt_access", "jt_refresh"]) {
    const v = byName.get(name)?.value ?? "";
    expect(v.length).toBeGreaterThan(0);
    expect(stored).not.toContain(v);
  }

  const body = { type: "run_analysis", params: {} };
  const noCsrf = await page.request.post("/api/v1/jobs", { data: body });
  expect(noCsrf.status()).toBe(403);
  expect((await noCsrf.json()).error?.code).toBe("csrf_failed");
  const wrongCsrf = await page.request.post("/api/v1/jobs", { data: body, headers: { "X-CSRF-Token": "wrong" } });
  expect(wrongCsrf.status()).toBe(403);
  const withCsrf = await page.request.post("/api/v1/jobs", {
    data: body,
    headers: { "X-CSRF-Token": byName.get("jt_csrf")?.value ?? "" },
  });
  expect(withCsrf.status()).toBe(202);

  await page.getByRole("button", { name: "로그아웃" }).click();
  await expect(page).toHaveURL(/\/login/);
  expect((await page.request.get("/api/v1/auth/me")).status()).toBe(401);

  await page.goto("/results");
  await expect(page).toHaveURL(/\/login\?next=%2Fresults/);
});

test("expired access cookie is refreshed once; reusing a rotated refresh token ends the session; tenants are isolated", async ({
  page,
  context,
  baseURL,
}) => {
  await signup(page, uniqueEmail("refresh"), `E2E 갱신 ${Date.now()}`);
  const cookie = async (name: string) => (await context.cookies()).find((c) => c.name === name)?.value ?? "";
  const oldRefresh = await cookie("jt_refresh");
  const csrf = await cookie("jt_csrf");

  // 다른 회사의 결정은 보이지 않는다(서버가 쿠키의 회사로만 조회).
  const mine = await (await page.request.get("/api/v1/decisions")).json();
  expect(mine.items).toEqual([]);
  if (firstTenantDecisionId) {
    const other = await page.request.get(`/api/v1/decisions/${encodeURIComponent(firstTenantDecisionId)}`);
    expect(other.status()).toBe(404);
  }

  // access 쿠키가 없어진 상태(만료와 같음)에서 보호 화면을 열면 refresh 한 번으로 이어진다.
  await context.clearCookies({ name: "jt_access" });
  const refreshed = page.waitForResponse((r) => r.url().endsWith("/api/v1/auth/refresh"));
  await page.goto("/results");
  expect((await refreshed).status()).toBe(200);
  await expect(page.getByRole("heading", { name: "결과 목록" })).toBeVisible();
  await expect(page).toHaveURL(/\/results/);
  const newRefresh = await cookie("jt_refresh");
  expect(newRefresh).not.toBe("");
  expect(newRefresh).not.toBe(oldRefresh);

  // 회전된(이전) refresh 토큰을 다시 쓰면 401 이고, 같은 묶음의 새 토큰도 폐기된다.
  const thief = await apiRequest.newContext({ baseURL });
  const reuse = await thief.post("/api/v1/auth/refresh", {
    headers: { Cookie: `jt_refresh=${oldRefresh}; jt_csrf=${csrf}`, "X-CSRF-Token": csrf },
  });
  expect(reuse.status()).toBe(401);
  await thief.dispose();

  await context.clearCookies({ name: "jt_access" });
  await page.goto("/results");
  await expect(page).toHaveURL(/\/login\?next=%2Fresults/);
});
