import { defineConfig, devices } from "@playwright/test";

/**
 * 화면 → Next rewrites → API → worker 를 실제 프로세스로 띄워 확인하는 E2E.
 * - API+worker: e2e/backend.mjs (JETTAE_ENV=test, 폐기용 SQLite, 마이그레이션 후 실행, LLM 오프라인)
 * - 화면: e2e/web.mjs (E2E API 주소로 빌드한 Next 를 next start)
 * 전제: 저장소 루트에서 `uv sync --all-extras`, 여기서 `npm ci` 와 `npm run e2e:install`.
 * 포트: E2E_API_PORT(기본 18000), E2E_WEB_PORT(기본 13000). 이미 쓰는 포트면 시작하지 않는다
 * (다른 서버를 E2E 대상으로 착각하지 않도록 reuseExistingServer=false).
 */
const API_PORT = process.env.E2E_API_PORT ?? "18000";
const WEB_PORT = process.env.E2E_WEB_PORT ?? "13000";
const env = { E2E_API_PORT: API_PORT, E2E_WEB_PORT: WEB_PORT };

export default defineConfig({
  testDir: "./e2e",
  outputDir: ".e2e-tmp/results",
  // 한 DB 를 공유하고 worker 가 하나라 흐름 테스트는 순서대로 돌린다.
  fullyParallel: false,
  workers: 1,
  retries: 0,
  forbidOnly: Boolean(process.env.CI),
  timeout: 180_000,
  expect: { timeout: 30_000 },
  reporter: [["list"], ["html", { outputFolder: ".e2e-tmp/report", open: "never" }]],
  use: {
    baseURL: `http://127.0.0.1:${WEB_PORT}`,
    trace: "retain-on-failure",
    locale: "ko-KR",
    timezoneId: "Asia/Seoul",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: [
    {
      name: "api+worker",
      command: "node e2e/backend.mjs",
      url: `http://127.0.0.1:${API_PORT}/api/v1/health`,
      timeout: 180_000,
      reuseExistingServer: false,
      stdout: "pipe",
      env,
    },
    {
      name: "web",
      command: "node e2e/web.mjs",
      url: `http://127.0.0.1:${WEB_PORT}/login`,
      timeout: 600_000,
      reuseExistingServer: false,
      stdout: "pipe",
      env,
    },
  ],
});
