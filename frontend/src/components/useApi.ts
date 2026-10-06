"use client";

import { useCallback, useEffect, useEffectEvent, useState } from "react";

export interface ApiState<T> {
  data: T | undefined;
  error: unknown;
  loading: boolean;
  reload: () => void;
  setData: (d: T) => void;
}

/**
 * key 가 바뀌거나 reload() 를 부르면 fn 을 다시 실행한다. key 가 null 이면 실행하지 않는다.
 * 상태 갱신은 Promise 콜백에서만 일어난다(렌더링·이펙트 본문에서 동기 setState 없음).
 */
export function useApi<T>(key: string | null, fn: () => Promise<T>): ApiState<T> {
  const [tick, setTick] = useState(0);
  const [state, setState] = useState<{ token: string; data?: T; error: unknown }>({
    token: "",
    error: null,
  });
  const token = key === null ? "" : `${key}#${tick}`;
  const run = useEffectEvent(fn);

  useEffect(() => {
    if (key === null) return;
    let alive = true;
    const t = `${key}#${tick}`;
    run().then(
      (data) => {
        if (alive) setState({ token: t, data, error: null });
      },
      (error: unknown) => {
        if (alive) setState((s) => ({ token: t, data: s.data, error }));
      },
    );
    return () => {
      alive = false;
    };
  }, [key, tick]);

  const reload = useCallback(() => setTick((n) => n + 1), []);
  const setData = useCallback((d: T) => setState((s) => ({ ...s, data: d })), []);
  return {
    data: state.data,
    error: state.token === token ? state.error : null,
    loading: key !== null && state.token !== token,
    reload,
    setData,
  };
}
