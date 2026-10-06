// E2E 백엔드: 폐기 가능한 SQLite DB 에 마이그레이션을 적용한 뒤 API 와 worker 를 함께 띄운다.
// Playwright webServer 가 실행하고, 테스트가 끝나면 프로세스 트리째 종료한다.
//
// - 매 실행마다 DB·파일 저장소를 지우고 새로 만든다(이전 실행의 자료가 결과에 섞이지 않게).
// - 개발자 셸의 JETTAE_* 와 LLM API 키를 물려받지 않는다. 실수로 live 모드·유료 호출·다른 DB 를
//   쓰지 않게 하기 위해서다. cwd 도 임시 폴더라 저장소의 ./.env 를 읽지 않는다.
// - 파일 저장소는 OS 임시 폴더에 둔다. 이 저장소 경로가 길어 Windows(긴 경로 비활성)에서 Python 이
//   260자를 넘는 blob 경로를 만들지 못하기 때문이다. DB 파일은 frontend/.e2e-tmp 아래에 둔다.
// - uv 실행 파일: JETTAE_UV > UV > PATH 의 uv. 먼저 `uv sync --all-extras` 로 환경을 만들어 둔다.
import { spawn, spawnSync } from "node:child_process";
import { randomBytes } from "node:crypto";
import { mkdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const frontend = resolve(here, "..");
const repo = resolve(frontend, "..");
const port = process.env.E2E_API_PORT ?? "18000";
const uv = process.env.JETTAE_UV || process.env.UV || "uv";

const runDir = join(frontend, ".e2e-tmp", "backend");
const scratch = join(tmpdir(), `jettae-e2e-${port}`);
for (const d of [runDir, scratch]) {
  rmSync(d, { recursive: true, force: true });
  mkdirSync(d, { recursive: true });
}

const env = {};
for (const [k, v] of Object.entries(process.env)) {
  if (k.startsWith("JETTAE_") || /(^|_)API_KEY$/.test(k)) continue;
  env[k] = v;
}
Object.assign(env, {
  JETTAE_ENV: "test",
  JETTAE_DATABASE_URL: `sqlite:///${join(runDir, "e2e.db").replaceAll("\\", "/")}`,
  JETTAE_BLOB_DIR: join(scratch, "blobs"),
  JETTAE_STATE_DIR: join(scratch, "state"),
  JETTAE_LLM_CACHE_DIR: join(scratch, "llm"),
  // 실행마다 새로 만드는 테스트 전용 서명 키. 파일·로그에 남기지 않는다.
  JETTAE_JWT_SECRET: randomBytes(48).toString("base64url"),
  JETTAE_LLM_MODE: "offline",
  JETTAE_LLM_BUDGET_KRW: "0",
  // 한 IP 에서 가입·로그인을 여러 번 하므로 IP 제한을 끈다(제한 동작 자체는 백엔드 테스트가 확인).
  JETTAE_AUTH_IP_PER_MINUTE: "0",
  JETTAE_PUBLIC_IP_PER_MINUTE: "0",
  // refresh 재사용 유예(기본 20초)를 2초로 줄여, 유예 안의 겹친 갱신과 유예 뒤의 재사용(세션 종료)을
  // 한 테스트에서 모두 확인한다.
  JETTAE_REFRESH_REUSE_GRACE_S: "2",
  JETTAE_WORKER_POLL_S: "0.3",
  PYTHONIOENCODING: "utf-8",
  PYTHONUTF8: "1",
});

const uvArgs = (...a) => ["run", "--project", repo, "--no-sync", "jettae", ...a];

const mig = spawnSync(uv, uvArgs("api", "migrate"), { cwd: runDir, env, stdio: "inherit" });
if (mig.error || mig.status !== 0) {
  console.error(`[e2e] migration failed (${mig.error?.message ?? `exit ${mig.status}`}); uv=${uv}`);
  process.exit(1);
}

const children = [
  spawn(uv, uvArgs("api", "serve", "--host", "127.0.0.1", "--port", port, "--log-level", "warning"), {
    cwd: runDir,
    env,
    stdio: "inherit",
  }),
  spawn(uv, uvArgs("worker", "run", "--log-level", "WARNING"), { cwd: runDir, env, stdio: "inherit" }),
];

let stopping = false;
function stop(code) {
  if (stopping) return;
  stopping = true;
  for (const c of children) if (c.exitCode === null) c.kill();
  setTimeout(() => process.exit(code), 500);
}
for (const c of children) {
  // 둘 중 하나라도 죽으면 E2E 를 계속할 수 없다(작업이 처리되지 않거나 API 가 없음).
  c.on("exit", (code) => {
    if (!stopping) console.error(`[e2e] backend process exited (${code}); stopping`);
    stop(code ?? 1);
  });
}
process.on("SIGINT", () => stop(0));
process.on("SIGTERM", () => stop(0));
