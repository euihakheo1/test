"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";

const APP_VERSION = process.env.NEXT_PUBLIC_APP_VERSION ?? "dev";

/** 버전·기준 시각 표시: 화면 버전, 서버 API 버전(openapi info.version), 표시 시간대. */
export function Footer() {
  const [apiVersion, setApiVersion] = useState<string | null | undefined>(undefined);
  useEffect(() => {
    let alive = true;
    api.apiVersion().then((v) => {
      if (alive) setApiVersion(v);
    });
    return () => {
      alive = false;
    };
  }, []);
  return (
    <footer className="foot">
      <div className="foot-in">
        <span>화면 버전 {APP_VERSION}</span>
        <span>
          서버 API 버전{" "}
          {apiVersion === undefined ? "확인 중" : apiVersion === null ? "확인 불가(서버 연결 실패)" : apiVersion}
        </span>
        <span>API 주소 같은 출처 /api/v1</span>
        <span>시각 표시: 한국 시간(Asia/Seoul). 업무 날짜는 변환하지 않음.</span>
        <span>결과는 차이·필요 서류·확인이 필요한 조건·관련 근거만 보여 주며 법적 판단이 아닙니다.</span>
      </div>
    </footer>
  );
}
