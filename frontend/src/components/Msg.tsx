import type { ReactNode } from "react";
import { MSG_TITLE, type MsgKind } from "@/lib/fmt";

/** 메시지 상자: 처리 실패 / 자료 없음 / 판단 보류 / 안내 / 완료를 서로 다른 모양으로. */
export function Msg({ kind, title, children }: { kind: MsgKind; title?: string; children?: ReactNode }) {
  return (
    <div className={`msg ${kind}`} role={kind === "failure" ? "alert" : "status"}>
      <span className="msg-title">{title ?? MSG_TITLE[kind]}</span>
      {children}
    </div>
  );
}

export function Loading({ text = "불러오는 중" }: { text?: string }) {
  return (
    <p className="muted" role="status">
      <span className="spin" aria-hidden /> {text}…
    </p>
  );
}
