"use client";

import { useCallback, useState } from "react";

export const MAX_COMPARE = 3;

/** Local UI state: which candidates are selected for side-by-side comparison. */
export function useCandidateCompare(max: number = MAX_COMPARE) {
  const [selectedIds, setSelectedIds] = useState<readonly string[]>([]);

  const toggle = useCallback(
    (id: string) =>
      setSelectedIds((prev) => {
        if (prev.includes(id)) return prev.filter((x) => x !== id);
        return prev.length >= max ? prev : [...prev, id];
      }),
    [max],
  );

  const clear = useCallback(() => setSelectedIds([]), []);

  return { selectedIds, toggle, clear, isFull: selectedIds.length >= max };
}
