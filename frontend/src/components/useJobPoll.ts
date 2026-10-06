"use client";

import { useEffect, useState } from "react";
import { api, type Job } from "@/lib/api";

export const TERMINAL = new Set(["succeeded", "failed", "cancelled"]);

/**
 * 작업 상태를 끝날 때까지 주기적으로 읽는다(1초에서 시작해 최대 5초 간격).
 * 화면을 떠나면 멈춘다. 네트워크 오류는 기록만 하고 계속 시도한다.
 */
export function useJobPoll(jobId: string | null): {
  job: Job | null;
  error: unknown;
  polls: number;
  refresh: (j: Job) => void;
} {
  const [state, setState] = useState<{ id: string | null; job: Job | null; error: unknown; polls: number }>({
    id: null,
    job: null,
    error: null,
    polls: 0,
  });

  useEffect(() => {
    if (!jobId) return;
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const ctl = new AbortController();
    let delay = 1000;
    const tick = async () => {
      try {
        const j = await api.job(jobId, ctl.signal);
        if (!alive) return;
        setState((s) => ({ id: jobId, job: j, error: null, polls: (s.id === jobId ? s.polls : 0) + 1 }));
        if (TERMINAL.has(j.status)) return;
      } catch (e) {
        if (!alive) return;
        setState((s) => ({ ...s, id: jobId, error: e, polls: (s.id === jobId ? s.polls : 0) + 1 }));
      }
      delay = Math.min(5000, Math.round(delay * 1.4));
      timer = setTimeout(tick, delay);
    };
    void tick();
    return () => {
      alive = false;
      ctl.abort();
      if (timer) clearTimeout(timer);
    };
  }, [jobId]);

  const same = state.id === jobId;
  return {
    job: same ? state.job : null,
    error: same ? state.error : null,
    polls: same ? state.polls : 0,
    refresh: (j: Job) => setState((s) => ({ ...s, job: j })),
  };
}
