import type { Run } from "./types";

/**
 * Static placeholder data so the UI renders before the runs API/SSE layer is
 * wired up. Replace with `useRunState` / API data in the next step.
 */
export const MOCK_RUNS: readonly Run[] = [
  {
    id: "run_demo_001",
    goal: "Buy a 27-inch 4K monitor under $350 with delivery by Friday",
    status: "awaiting_approval",
    created_at: "2026-11-11T09:12:00Z",
    updated_at: "2026-11-11T09:15:00Z",
    budget_usd: 350,
    iteration: 3,
    max_iterations: 10,
    pending_approval: {
      run_id: "run_demo_001",
      item: "Dell S2725QC 27-inch 4K Monitor",
      price_usd: 329.99,
      seller: "Dell Official Store",
      delivery_by: "2026-11-13T00:00:00Z",
      terms_hash: "9f2c1ab47e03d6685b1c0a4f7d92e8135ac6f0b2d47e91c8a3f5b60d1e7c2a94",
    },
    steps: [
      {
        id: "s1",
        title: "Extract constraints",
        agent: "intent",
        status: "done",
        started_at: "2026-11-11T09:12:05Z",
        summary: "Hard: price ≤ $350, delivery ≤ Friday. Soft: 27-inch, 4K.",
        output: { "Max price": "$350.00", Delivery: "By Friday" },
      },
      {
        id: "s2",
        title: "Search merchants",
        agent: "discovery",
        status: "done",
        started_at: "2026-11-11T09:12:40Z",
        summary: "Collected 14 listings across 3 platforms.",
        output: { Listings: "14", Platforms: "3" },
      },
      {
        id: "s3",
        title: "Rank candidates",
        agent: "evaluator",
        status: "done",
        started_at: "2026-11-11T09:14:00Z",
        summary: "4 viable candidates; 10 rejected on price or delivery.",
        output: { Viable: "4", Rejected: "10" },
      },
      {
        id: "s4",
        title: "Await user approval",
        agent: "executor",
        status: "running",
        started_at: "2026-11-11T09:15:00Z",
        summary: "Waiting for approval of the selected offer.",
      },
      { id: "s5", title: "Authorize payment", agent: "payment", status: "pending" },
    ],
  },
  {
    id: "run_demo_002",
    goal: "Reorder printer ink cartridges",
    status: "completed",
    created_at: "2026-11-10T14:00:00Z",
    updated_at: "2026-11-10T14:09:00Z",
    budget_usd: 60,
    iteration: 2,
    max_iterations: 10,
    steps: [
      { id: "s1", title: "Extract constraints", agent: "intent", status: "done" },
      { id: "s2", title: "Search merchants", agent: "discovery", status: "done" },
      { id: "s3", title: "Verify and capture", agent: "verifier", status: "done" },
    ],
  },
];

export function getMockRun(runId: string): Run | undefined {
  return MOCK_RUNS.find((run) => run.id === runId);
}
