import type { Entity } from "../api";
import type { WorkspaceSummary } from "./jobs";

type StatusRow = { status: string; count: number };
const filterableStatuses = new Set([
  "QUEUED",
  "RUNNING",
  "VERIFYING",
  "CANCELLING",
  "SUCCEEDED",
  "PARTIAL",
  "FAILED",
  "CANCELLED",
  "TIMED_OUT",
]);

// Preserve new server categories in the chart without sending unsupported filter enums.
export function statusFilterFor(status: string) {
  return filterableStatuses.has(status) ? status : "ALL";
}
type StatusDistribution = {
  kind: "unavailable" | "empty" | "ready" | "incomplete";
  rows: StatusRow[];
  countedTotal: number;
  expectedTotal: number | null;
};

export function buildStatusDistribution(
  summary: WorkspaceSummary | null,
): StatusDistribution {
  const counts = summary?.job_status_counts;
  const expectedTotal =
    summary && Number.isSafeInteger(summary.jobs) && summary.jobs >= 0
      ? summary.jobs
      : null;
  if (!counts || typeof counts !== "object" || Array.isArray(counts))
    return { kind: "unavailable", rows: [], countedTotal: 0, expectedTotal };

  const order = [
    "RUNNING",
    "VALIDATING",
    "VERIFYING",
    "CANCELLING",
    "QUEUED",
    "PENDING",
    "PLANNING",
    "CREATED",
    "SUCCEEDED",
    "PARTIAL",
    "FAILED",
    "CANCELLED",
    "TIMED_OUT",
    "TIMEOUT",
    "WORKER_LOST",
    "RESOURCE_LIMIT",
  ];
  const rows: StatusRow[] = [];
  let invalid = false;
  let countedTotal = 0;
  for (const [status, count] of Object.entries(counts)) {
    if (
      !status.trim() ||
      !Number.isSafeInteger(count) ||
      count < 0 ||
      !Number.isSafeInteger(countedTotal + count)
    ) {
      invalid = true;
      continue;
    }
    rows.push({ status, count });
    countedTotal += count;
  }
  rows.sort((a, b) => {
    const rank = (status: string) => {
      const index = order.indexOf(status);
      return index < 0 ? order.length : index;
    };
    return rank(a.status) - rank(b.status) || a.status.localeCompare(b.status);
  });
  const complete =
    !invalid && expectedTotal !== null && countedTotal === expectedTotal;
  return {
    kind: !complete ? "incomplete" : countedTotal === 0 ? "empty" : "ready",
    rows,
    countedTotal,
    expectedTotal,
  };
}

export function buildRecentActivity(
  jobs: Entity[],
  total: number | undefined,
  hasMore: boolean,
  limit = 4,
): {
  items: { job: Entity; createdAt: number | null }[];
  scope: "all" | "loaded";
  loadedCount: number;
  totalCount: number | null;
} {
  const totalCount =
    total !== undefined && Number.isSafeInteger(total) && total >= jobs.length
      ? total
      : null;
  const items = jobs.map((job, index) => {
    const parsed =
      typeof job.created_at === "string" && job.created_at.trim()
        ? Date.parse(job.created_at)
        : NaN;
    return { job, index, createdAt: Number.isFinite(parsed) ? parsed : null };
  });
  // A malformed date cannot outrank a valid creation instant or mutate API order.
  items.sort(
    (a, b) =>
      (b.createdAt ?? -Infinity) - (a.createdAt ?? -Infinity) ||
      a.index - b.index,
  );
  const count = Number.isFinite(limit) ? Math.max(0, Math.floor(limit)) : 4;
  return {
    items: items
      .slice(0, count)
      .map(({ job, createdAt }) => ({ job, createdAt })),
    scope: !hasMore && totalCount === jobs.length ? "all" : "loaded",
    loadedCount: jobs.length,
    totalCount,
  };
}
