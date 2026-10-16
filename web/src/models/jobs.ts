import type { Entity } from "../api";

export type JobPage = {
  items: Entity[];
  next_cursor: string | null;
  total: number;
};
export type EventPage = {
  items: Entity[];
  next_after: number;
  has_more: boolean;
};
export type WorkspaceSummary = {
  projects: number;
  matrices: number;
  jobs: number;
  active_jobs: number;
  certified_jobs: number;
  job_status_counts: Record<string, number>;
};
export type JobList = JobPage & { firstPageIds: string[]; loadedMore: boolean };
export type DetailState = {
  id: string;
  job: Entity | null;
  events: Entity[];
  after: number;
  result: Entity | null;
  truncated: boolean;
};
export const EVENT_WINDOW_LIMIT = 1000;
export const activeStatuses = new Set([
  "CREATED",
  "QUEUED",
  "PENDING",
  "RUNNING",
  "VALIDATING",
  "VERIFYING",
  "PLANNING",
  "CANCELLING",
]);
const terminalStatuses = new Set([
  "SUCCEEDED",
  "PARTIAL",
  "FAILED",
  "CANCELLED",
  "TIMED_OUT",
]);

export function emptyDetail(
  id: string,
  job: Entity | null = null,
): DetailState {
  return {
    id,
    job,
    events: [],
    after: 0,
    result: job?.result || null,
    truncated: false,
  };
}

export function mergeEventWindow(current: Entity[], incoming: Entity[]) {
  const bySequence = new Map<number, Entity>();
  for (const event of [...current, ...incoming]) {
    if (Number.isSafeInteger(event.seq) && event.seq > 0)
      bySequence.set(event.seq, event);
  }
  const ordered = [...bySequence.values()].sort((a, b) => a.seq - b.seq);
  return {
    items: ordered.slice(-EVENT_WINDOW_LIMIT),
    truncated: ordered.length > EVENT_WINDOW_LIMIT,
  };
}

export function jobPagePath(
  projectId: string,
  status: string,
  cursor?: string | null,
) {
  const params = new URLSearchParams({ limit: "50" });
  if (projectId) params.set("project_id", projectId);
  if (status !== "ALL") params.set("status", status);
  if (cursor) params.set("cursor", cursor);
  return `/jobs/page?${params}`;
}

export function applyJobPage(
  current: JobList | null,
  page: JobPage,
  mode: "refresh" | "append",
  projectId: string,
  status: string,
): JobList {
  if (!current || (mode === "refresh" && !current.loadedMore)) {
    return {
      ...page,
      firstPageIds: page.items.map((j) => j.id),
      loadedMore: false,
    };
  }
  const oldFirst = new Set(current.firstPageIds);
  const candidates =
    mode === "append"
      ? [...current.items, ...page.items]
      : [...page.items, ...current.items.filter((j) => !oldFirst.has(j.id))];
  const byId = new Map<string, Entity>();
  for (const job of candidates) {
    if (
      (!projectId || job.project_id === projectId) &&
      (status === "ALL" || job.status === status)
    ) {
      if (mode === "append" || !byId.has(job.id)) byId.set(job.id, job);
    }
  }
  return {
    items: [...byId.values()],
    next_cursor: mode === "append" ? page.next_cursor : current.next_cursor,
    total: page.total,
    firstPageIds:
      mode === "refresh" ? page.items.map((j) => j.id) : current.firstPageIds,
    loadedMore: true,
  };
}

export function listPollDelay(activeJobs: number) {
  return activeJobs > 0 ? 3500 : 15000;
}

type Request = <T = Entity>(path: string, init: RequestInit) => Promise<T>;
export async function readDetailCycle(
  jobId: string,
  state: DetailState,
  request: Request,
  signal: AbortSignal,
): Promise<{ state: DetailState; delay: number | null }> {
  signal.throwIfAborted();
  const eventPath = (after: number) =>
    `/jobs/${jobId}/events/page?after=${after}&limit=200`;
  // Events must be read after the job state, so a terminal status cannot outrun its final event.
  const job = await request<Entity>(`/jobs/${jobId}`, { signal });
  signal.throwIfAborted();
  const firstPage = await request<EventPage>(eventPath(state.after), {
    signal,
  });
  let next = { ...state, job, result: job.result || state.result };
  let page = firstPage;
  let hasMore = false;
  for (let count = 0; count < 5; count++) {
    signal.throwIfAborted();
    const merged = mergeEventWindow(next.events, page.items);
    const after = Math.max(
      next.after,
      page.next_after,
      merged.items.at(-1)?.seq || 0,
    );
    if (page.has_more && after <= next.after)
      throw new Error("事件分页游标未前进，请重试连接");
    next = {
      ...next,
      events: merged.items,
      after,
      truncated: next.truncated || merged.truncated,
    };
    hasMore = page.has_more;
    if (!hasMore || count === 4) break;
    page = await request<EventPage>(eventPath(after), { signal });
  }
  if (hasMore) return { state: next, delay: 150 };
  if (!terminalStatuses.has(job.status)) return { state: next, delay: 2500 };
  try {
    next.result = await request<Entity>(`/jobs/${jobId}/results`, { signal });
  } catch (error) {
    const status = (error as { status?: number }).status;
    const noResult = ["FAILED", "CANCELLED", "TIMED_OUT"].includes(job.status);
    if (!noResult || (status !== 404 && status !== 409)) throw error;
  }
  signal.throwIfAborted();
  return { state: next, delay: null };
}
