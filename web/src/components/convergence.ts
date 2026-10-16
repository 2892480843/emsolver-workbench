import type { Entity } from "../api";

export type ResidualPoint = {
  seq: number;
  iteration: number | null;
  residual: number;
  locked: boolean | null;
  sigmaWork: number | null;
  segment: number;
};
export type HistoryMarker = {
  seq: number;
  kind: "failure" | "shift" | "lock" | "restart" | "fallback";
  label: string;
  count?: number;
};
const finite = (value: unknown): value is number =>
  typeof value === "number" && Number.isFinite(value);
const count = (value: unknown): value is number =>
  finite(value) && Number.isSafeInteger(value) && value >= 0;

// Each bucket keeps its own measured minimum and maximum; critical events have
// a separate unsampled channel and never compete for this curve point budget.
export function sampleExtrema<T extends { residual: number }>(
  points: T[],
  budget = 240,
): T[] {
  const limit = Math.max(4, Math.floor(budget));
  if (points.length <= limit) return points;
  const buckets = Math.floor((limit - 2) / 2);
  const retained = new Set([0, points.length - 1]);
  for (let bucket = 0; bucket < buckets; bucket++) {
    const start = 1 + Math.floor(((points.length - 2) * bucket) / buckets);
    const end = 1 + Math.floor(((points.length - 2) * (bucket + 1)) / buckets);
    let low = start,
      high = start;
    for (let i = start + 1; i < end; i++) {
      if (points[i].residual < points[low].residual) low = i;
      if (points[i].residual > points[high].residual) high = i;
    }
    retained.add(low);
    retained.add(high);
  }
  return [...retained].sort((a, b) => a - b).map((i) => points[i]);
}

export function convergenceHistory(
  events: Entity[],
  firstRank = 0,
  rankCount = 6,
  pointBudget = 240,
) {
  const start = Math.max(0, Math.floor(firstRank));
  const width = Math.max(1, Math.min(8, Math.floor(rankCount)));
  const series = Array.from({ length: width }, (_, index) => ({
    rank: start + index,
    points: [] as ResidualPoint[],
    measuredCount: 0,
  }));
  const markers: HistoryMarker[] = [];
  const attempts: { seq: number; residual: number }[] = [];
  const stalls: {
    start: number;
    end: number;
    checks: number;
    threshold: number | null;
  }[] = [];
  let modeCount = 0,
    sigmaWork: number | null = null,
    previousLocks: number | null = null,
    previousLockSet: string | null = null,
    segment = 0,
    stagnationAvailable = false;
  let stall: (typeof stalls)[number] | null = null;
  for (const [index, event] of events.entries()) {
    const seq = count(event.seq) ? event.seq : index + 1;
    const name = String(event.event || "");
    const metrics = event.metrics || {};
    if (name === "shift_attempt") {
      segment++;
      stall = null;
      sigmaWork = finite(event.sigma_work) ? event.sigma_work : null;
      markers.push({
        seq,
        kind: "shift",
        label: `工作位移 ${sigmaWork ?? "不可用"}${finite(event.sigma_target) ? `；原目标 ${event.sigma_target}` : ""}`,
      });
    } else if (name.includes("fallback")) {
      segment++;
      stall = null;
      markers.push({
        seq,
        kind: "fallback",
        label: `${name}${metrics.profile ? ` · ${metrics.profile}` : ""}${metrics.reason ? ` · ${metrics.reason}` : ""}`,
      });
    } else if (name === "block_restarted") {
      segment++;
      stall = null;
      const reason = event.restart_reason || metrics.reason;
      markers.push({
        seq,
        kind: "restart",
        label: `子空间重启${reason ? ` · ${reason}` : ""}`,
        ...(count(metrics.restarts) ? { count: metrics.restarts } : {}),
      });
    } else if (
      name.includes("failed") ||
      event.error_code ||
      ["FAILED", "CANCELLED", "TIMED_OUT"].includes(event.status)
    ) {
      markers.push({
        seq,
        kind: "failure",
        label: `${name}${event.error_code ? ` · ${event.error_code}` : ""}`,
      });
    }
    const indices: number[] | null = Array.isArray(event.locked_indices)
      ? event.locked_indices.filter(count)
      : null;
    const lockSet = indices ? indices.join(",") : null;
    if (
      (count(metrics.soft_locked_modes) &&
        metrics.soft_locked_modes !== previousLocks) ||
      (lockSet !== null && lockSet !== previousLockSet)
    ) {
      if (count(metrics.soft_locked_modes))
        previousLocks = metrics.soft_locked_modes;
      previousLockSet = lockSet;
      markers.push({
        seq,
        kind: "lock",
        label: indices
          ? `软锁定候选排序位置：${
              indices.length
                ? indices
                    .slice(0, 12)
                    .map((rank) => rank + 1)
                    .join(", ")
                : "无"
            }${indices.length > 12 ? "…（完整位置见原事件）" : ""}`
          : "已报告软锁定数量（含内部超采样候选）",
        ...(count(metrics.soft_locked_modes)
          ? { count: metrics.soft_locked_modes }
          : {}),
      });
    }
    if (finite(event.residual_max) && event.residual_max >= 0)
      attempts.push({ seq, residual: event.residual_max });
    if (
      !Array.isArray(event.residuals) ||
      !event.residuals.some((value: unknown) => finite(value) && value >= 0)
    )
      continue;
    modeCount = Math.max(modeCount, event.residuals.length);
    const locked = indices ? new Set(indices) : null;
    for (const line of series) {
      const residual = event.residuals[line.rank];
      if (!finite(residual) || residual < 0) continue;
      line.points.push({
        seq,
        iteration: count(event.iteration) ? event.iteration : null,
        residual,
        locked: locked ? locked.has(line.rank) : null,
        sigmaWork,
        segment,
      });
    }
    const checks = event.stalled_checks ?? metrics.stalled_checks;
    if (count(checks)) {
      stagnationAvailable = true;
      if (checks > 0) {
        if (!stall) {
          stall = {
            start: seq,
            end: seq,
            checks,
            threshold: count(event.stagnation_threshold)
              ? event.stagnation_threshold
              : null,
          };
          stalls.push(stall);
        } else {
          stall.end = seq;
          stall.checks = checks;
        }
      } else stall = null;
    } else stall = null;
  }
  for (const line of series) {
    line.measuredCount = line.points.length;
    line.points = sampleExtrema(line.points, pointBudget);
  }
  return {
    series: series.filter((line) => line.points.length),
    modeCount,
    markers,
    attempts: sampleExtrema(attempts, pointBudget),
    stalls,
    stagnationAvailable,
  };
}
