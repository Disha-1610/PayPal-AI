import { ApiError, getRun, listRuns, type ListRunsParams } from "@/lib/api";
import { MOCK_RUNS, getMockRun } from "./mock";
import type { Run, RunPage } from "./types";

/** Serve static demo data instead of the API (backend runs routes are not built yet). */
const USE_MOCKS = process.env.NEXT_PUBLIC_USE_MOCKS === "true";

export async function fetchRunPage(params: ListRunsParams, signal: AbortSignal): Promise<RunPage> {
  if (USE_MOCKS) return { items: [...MOCK_RUNS], next_cursor: null };
  return listRuns(params, signal);
}

export async function fetchRun(runId: string, signal: AbortSignal): Promise<Run> {
  if (!USE_MOCKS) return getRun(runId, signal);
  const run = getMockRun(runId);
  if (!run) throw new ApiError(404, "run not found");
  return run;
}
