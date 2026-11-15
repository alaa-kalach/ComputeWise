// Load something once (and again on reload()). Keeps pages free of loading/error plumbing.

import { useCallback, useEffect, useRef, useState } from "react";

export interface Async<T> {
  data: T | undefined;
  error: Error | undefined;
  loading: boolean;
  reload: () => Promise<void>;
}

export function useAsync<T>(fn: () => Promise<T>, deps: unknown[] = []): Async<T> {
  const [data, setData] = useState<T>();
  const [error, setError] = useState<Error>();
  const [loading, setLoading] = useState(true);
  const live = useRef(true);

  // eslint-disable-next-line react-hooks/exhaustive-deps
  const run = useCallback(fn, deps);

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      const value = await run();
      if (live.current) {
        setData(value);
        setError(undefined);
      }
    } catch (e) {
      if (live.current) setError(e instanceof Error ? e : new Error(String(e)));
    } finally {
      if (live.current) setLoading(false);
    }
  }, [run]);

  useEffect(() => {
    live.current = true;
    void reload();
    return () => {
      live.current = false;
    };
  }, [reload]);

  return { data, error, loading, reload };
}
