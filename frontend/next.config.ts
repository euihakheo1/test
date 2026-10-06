import type { NextConfig } from "next";
import pkg from "./package.json" with { type: "json" };

/**
 * API 연결: 브라우저는 항상 같은 출처의 /api/v1/* 를 부르고, 아래 rewrites 가 JETTAE_API_ORIGIN
 * (기본 http://127.0.0.1:8000)으로 넘긴다.
 * - 같은 출처여야 하는 이유: 인증은 서버가 심는 HttpOnly 쿠키이고, CSRF 토큰 쿠키(jt_csrf)를 화면
 *   스크립트가 읽어 X-CSRF-Token 헤더로 돌려보내야 한다. 다른 출처의 API 를 직접 부르면 그 쿠키를
 *   읽을 수 없다.
 * - rewrites 대상은 `next build` 때 routes-manifest 에 고정된다. `next start` 때만 바꾸면 반영되지 않는다.
 * - JETTAE_NEXT_DIST_DIR / JETTAE_NEXT_TSCONFIG: E2E(e2e/web.mjs)만 쓴다. 운영 빌드(.next)를 덮어쓰지
 *   않도록 다른 빌드 폴더를 쓰고, next build 가 빌드 폴더의 타입 경로를 추가하려고 tsconfig.json 을
 *   고쳐 쓰지 않도록 그 경로를 미리 넣어 둔 tsconfig.e2e.json 을 쓴다.
 */
const apiOrigin = (process.env.JETTAE_API_ORIGIN ?? "http://127.0.0.1:8000").replace(/\/+$/, "");

const nextConfig: NextConfig = {
  poweredByHeader: false,
  distDir: process.env.JETTAE_NEXT_DIST_DIR || ".next",
  typescript: { tsconfigPath: process.env.JETTAE_NEXT_TSCONFIG || "tsconfig.json" },
  env: {
    NEXT_PUBLIC_APP_VERSION: pkg.version,
  },
  async rewrites() {
    return [{ source: "/api/v1/:path*", destination: `${apiOrigin}/api/v1/:path*` }];
  },
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "no-referrer" },
          { key: "X-Frame-Options", value: "DENY" },
        ],
      },
    ];
  },
};

export default nextConfig;
