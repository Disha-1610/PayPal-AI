"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "@/lib/api";
import { fetchRunPage } from "@/features/runs/source";
import type { Run } from "@/features/runs/types";

const PAGE_SIZE = 20;

export interface UseRuns {
  runs: readonly Run[];
  loading: boolean;
  loadingMore: boolean;
  error: string | null;
  hasMore: boolean;
  loadMore: () => void;
  reload: () => void;
}

function describe(err: unknown): string {
  return err instanceof ApiError ? err.message : "Could not load runs";
}

/** Cursor-paginated list of runs. */
export function useRuns(): UseRuns {
  const [runs, setRuns] = useState<readonly Run[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reloadToken, setReloadToken] = useState(0);
  const moreController = useRef<AbortController | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    fetchRunPage({ limit: PAGE_SIZE }, controller.signal)
      .then((page) => {
        setRuns(page.items);
        setCursor(page.next_cursor);
        setLoading(false);
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted) return;
        setError(describe(err));
        setLoading(false);
      });
    return () => {
      controller.abort();
      moreController.current?.abort();
    };
  }, [reloadToken]);

  const loadMore = useCallback(() => {
    if (!cursor || moreController.current) return;
    const controller = new AbortController();
    moreController.current = controller;
    setLoadingMore(true);
    fetchRunPage({ limit: PAGE_SIZE, cursor }, controller.signal)
      .then((page) => {
        setRuns((prev) => {
          const seen = new Set(prev.map((r) => r.id));
          return [...prev, ...page.items.filter((r) => !seen.has(r.id))];
        });
        setCursor(page.next_cursor);
      })
      .catch((err: unknown) => {
        if (!controller.signal.aborted) setError(describe(err));
      })
      .finally(() => {
        moreController.current = null;
        setLoadingMore(false);
      });
  }, [cursor]);

  const reload = useCallback(() => setReloadToken((n) => n + 1), []);

  return { runs, loading, loadingMore, error, hasMore: cursor !== null, loadMore, reload };
}
