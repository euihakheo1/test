// E2E 화면: E2E 용 API 주소로 Next 를 빌드하고 `next start` 로 띄운다.
// rewrites 대상(JETTAE_API_ORIGIN)은 빌드 때 고정되므로 운영 빌드(.next)를 재사용할 수 없다.
// 빌드 폴더는 .e2e-tmp/next 로 분리해 `npm run build` 결과를 덮어쓰지 않는다.
// E2E_SKIP_BUILD=1 이면 이미 만든 .e2e-tmp/next 를 그대로 쓴다(같은 E2E_API_PORT 로 빌드한 경우만).
import { spawn, spawnSync } from "node:child_process";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const frontend = resolve(here, "..");
const apiPort = process.env.E2E_API_PORT ?? "18000";
const webPort = process.env.E2E_WEB_PORT ?? "13000";
const nextBin = createRequire(import.meta.url).resolve("next/dist/bin/next");

const env = {
  ...process.env,
  JETTAE_API_ORIGIN: `http://127.0.0.1:${apiPort}`,
  JETTAE_NEXT_DIST_DIR: ".e2e-tmp/next",
  JETTAE_NEXT_TSCONFIG: "tsconfig.e2e.json",
  NEXT_TELEMETRY_DISABLED: "1",
};

if (process.env.E2E_SKIP_BUILD !== "1") {
  const b = spawnSync(process.execPath, [nextBin, "build"], { cwd: frontend, env, stdio: "inherit" });
  if (b.status !== 0) {
    console.error(`[e2e] next build failed (exit ${b.status})`);
    process.exit(1);
  }
}

const child = spawn(process.execPath, [nextBin, "start", "-H", "127.0.0.1", "-p", webPort], {
  cwd: frontend,
  env,
  stdio: "inherit",
});
child.on("exit", (code) => process.exit(code ?? 1));
for (const s of ["SIGINT", "SIGTERM"]) process.on(s, () => child.kill());
