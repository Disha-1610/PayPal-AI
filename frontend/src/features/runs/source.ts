import { getRun, listRuns, type ListRunsParams } from "@/lib/api";
import type { Run, RunPage } from "./types";

export function fetchRunPage(params: ListRunsParams, signal: AbortSignal): Promise<RunPage> {
  return listRuns(params, signal);
}

export function fetchRun(runId: string, signal: AbortSignal): Promise<Run> {
  return getRun(runId, signal);
}
